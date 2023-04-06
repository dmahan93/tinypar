import os
import time
from typing import List, Tuple, Optional
from dataclasses import dataclass
from typing import Optional, Tuple
from socket import gethostname

from einops import rearrange

import torch
import torch.nn as nn
import torch.nn.functional as F

from apex.transformer import parallel_state
from apex.transformer import tensor_parallel
from apex.transformer.pipeline_parallel import get_forward_backward_func, build_model
from apex.transformer.pipeline_parallel.utils import (
    average_losses_across_data_parallel_group,
    setup_microbatch_calculator,
    _reconfigure_microbatch_calculator,
)

from apex.contrib.optimizers.distributed_fused_adam import DistributedFusedAdam
from apex.optimizers.fused_adam import FusedAdam


import torch._dynamo

torch._dynamo.allow_in_graph(rearrange)


def identity(x):
    return x

torch._dynamo.config.cache_size_limit = 1000

@dataclass
class NeoXArgs:
    hidden_size: int = 512
    layer_norm_eps: float = 1e-6
    num_hidden_layers: int = 8
    num_attention_heads: int = 8
    rotary_pct: float = 0.125
    max_position_embeddings: int = 2048
    rotary_emb_base: float = 10000.0
    intermediate_size: int = 2048
    hidden_act: str = "gelu"
    use_parallel_residual: bool = True

def precompute_freqs(dim: int, end: int, theta: float = 10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)
    freqs = torch.outer(t, freqs).float()
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)
    return torch.view_as_real(freqs_cis)


def reshape_for_broadcast(freqs, x_shape):
    ndim = len(x_shape)
    assert 0 <= 1 < ndim
    assert freqs.shape == (
        x_shape[1],
        x_shape[-2],
        x_shape[-1],
    ), f"{freqs.shape=} not compatible with {x_shape=}"
    shape = [d if i == 1 or i >= ndim - 2 else 1 for i, d in enumerate(x_shape)]
    return freqs.view(*shape)


def cmul(x, y):
    return torch.stack(
        [
            x[..., 0] * y[..., 0] - x[..., 1] * y[..., 1],
            x[..., 0] * y[..., 1] + x[..., 1] * y[..., 0],
        ],
        dim=-1,
    )


@torch.compile
def apply_rotary_emb(
    x: torch.Tensor,
    freqs: torch.Tensor,
) -> torch.Tensor:
    x_ = x.float().reshape(*x.shape[:-1], -1, 2)
    x_out = cmul(x_, freqs).flatten(3)
    return x_out.type_as(x)


def add_bias(x: Tuple[torch.tensor, Optional[torch.Tensor]]):
    x, bias = x
    if bias is not None:
        x = x + bias
    return x


class GPTNeoXAttention(nn.Module):
    def __init__(self, args: NeoXArgs, dtype: torch.dtype = torch.float32):
        super().__init__()
        tp_size = parallel_state.get_tensor_model_parallel_world_size()
        assert args.n_heads % tp_size == 0
        self.n_local_heads = args.num_attention_heads // tp_size
        self.head_dim = args.hidden_size // args.num_attention_heads

        self.query_key_value = tensor_parallel.ColumnParallelLinear(
            args.hidden_size,
            args.hidden_size * 3,
            bias=True,
            gather_output=False,
            params_dtype=dtype,
            sequence_parallel_enabled=True,
            no_async_tensor_model_parallel_allreduce=True,
        )

        self.dense = tensor_parallel.RowParallelLinear(
            args.hidden_size,
            args.hidden_size,
            bias=True,
            input_is_parallel=True,
            params_dtype=dtype,
            sequence_parallel_enabled=True,
        )

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        kv_freqs: torch.Tensor,
        q_freqs: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        seqlen, bsz, _ = x.shape

        x = x.contiguous()
        qkv = add_bias(self.query_key_value(x))
        qkv = rearrange(qkv, "s b (qkv hd) -> qkv s b hd", qkv=3)

        xq, xk, xv = qkv.unbind(0)

        xk = apply_rotary_emb(xk, freqs=kv_freqs)
        xq = apply_rotary_emb(xq, freqs=q_freqs)

        xk = rearrange(xk, "b s nh hd -> b nh s hd")
        xv = rearrange(xv, "b s nh hd -> b nh s hd")
        xq = rearrange(xq, "b s nh hd -> b nh s hd")

        causal = mask is None
        with torch.backends.cuda.sdp_kernel(
            enable_math=causal, enable_flash=True, enable_mem_efficient=False
        ):
            output = F.scaled_dot_product_attention(xq, xk, xv, is_causal=causal, mask=mask)
            output = rearrange(output, "b nh s hd -> s b (nh hd)").contiguous()
            return add_bias(self.wo(output))


class GPTNeoXMLP(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int,
        dtype: torch.dtype = torch.float32,
    ):
        super().__init__()

        self.dense_h_to_4h = tensor_parallel.ColumnParallelLinear(
            dim,
            hidden_dim,
            bias=False,
            gather_output=False,
            init_method=lambda x: x,
            params_dtype=dtype,
            sequence_parallel_enabled=True,
            no_async_tensor_model_parallel_allreduce=True,
        )

        self.dense_4h_to_h = tensor_parallel.RowParallelLinear(
            hidden_dim,
            dim,
            bias=False,
            input_is_parallel=True,
            init_method=lambda x: x,
            params_dtype=dtype,
            sequence_parallel_enabled=True,
        )

    def forward(self, x):
        return self.dense_4h_to_h(F.gelu(self.dense_h_to_4h(x)))


class TransformerBlock(nn.Module):
    def __init__(self, layer_id: int, args: NeoXArgs, dtype: torch.dtype):
        super().__init__()
        self.attention = GPTNeoXAttention(args, dtype=dtype)
        self.mlp = GPTNeoXMLP(
            dim=args.hidden_size,
            hidden_dim=args.intermediate_size,
            dtype=dtype,
        )
        self.layer_id = layer_id
        self.post_attention_layernorm = nn.LayerNorm(args.hidden_size, eps=args.layer_norm_eps)
        self.input_layernorm = nn.LayerNorm(args.hidden_size, eps=args.layer_norm_eps)

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        kv_freqs: torch.Tensor,
        q_freqs: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        x0 = self.input_layernorm(x)
        x1 = self.post_attention_layernorm(x)
        return x + self.attention(x0, start_pos, kv_freqs, q_freqs, mask) + self.feed_forward(x1)





class SplitNeoX(nn.Module):
    def __init__(self, args: NeoXArgs, dtype: torch.dtype = torch.float32):
        super().__init__()
        self.pp_rank = parallel_state.get_pipeline_model_parallel_rank()
        self.pp_world = parallel_state.get_pipeline_model_parallel_world_size()
        self.tp_rank = parallel_state.get_tensor_model_parallel_rank()
        self.tp_world = parallel_state.get_tensor_model_parallel_world_size()

        curr_rank_layers = args.n_layers // self.pp_world
        start_layer = self.pp_rank * curr_rank_layers

        self.layers = nn.ModuleList(
            [TransformerBlock(i + start_layer, args, dtype) for i in range(curr_rank_layers)]
        )
        self.freqs = precompute_freqs(args.dim // args.n_heads, args.max_seq_len * 2)

        if self.pp_rank == 0:
            self.embed_in = tensor_parallel.VocabParallelEmbedding(
                args.vocab_size, args.dim, params_dtype=dtype
            )

        if self.pp_rank == self.pp_world - 1:
            self.embed_output = tensor_parallel.ColumnParallelLinear(
                args.hidden_size,
                args.vocab_size,
                bias=False,
                params_dtype=dtype,
                gather_output=False,
                sequence_parallel_enabled=True,
                no_async_tensor_model_parallel_allreduce=True,
            )
            self.final_layer_norm = nn.LayerNorm(args.dim, eps=args.layer_norm_eps)

        self.args = args

    # factored out for torch.compile
    @torch.compile
    def transformer_block(self, x, start_pos, kv_freqs, q_freqs, mask):
        for layer in self.layers:
            x = layer(x, start_pos, kv_freqs, q_freqs, mask)
        return x

    def forward(self, tokens_or_hidden_state: torch.Tensor, start_pos: int):
        if self.pp_rank == 0:
            x = self.embed_in(tokens_or_hidden_state)
            x = rearrange(x, "b s d -> s b d")
            x = tensor_parallel.mappings.scatter_to_sequence_parallel_region(x)
        else:
            x = tokens_or_hidden_state

        seq_len, batch_size, _ = x.shape
        total_seq_len = seq_len * self.tp_world

        mask = torch.full((1, 1, seq_len, seq_len), float("-inf"), device=x.device)
        mask = torch.triu(mask, diagonal=start_pos + 1).type_as(x)

        kv_freqs = self.freqs[start_pos : start_pos + total_seq_len].to(x.device)
        sp_n_queries = seq_len // self.tp_world
        q_freqs = kv_freqs

        n_heads = self.args.num_attention_heads
        head_dim = self.args.hidden_size // n_heads
        kv_shape = (batch_size, total_seq_len, n_heads, head_dim // 2, 2)
        q_shape = (batch_size, total_seq_len, n_heads, head_dim // 2, 2)
        kv_freqs = reshape_for_broadcast(kv_freqs, kv_shape).to(x.device)
        q_freqs = reshape_for_broadcast(q_freqs, q_shape).to(x.device)

        x = self.transformer_block(x, start_pos, kv_freqs, q_freqs, mask=None)

        if self.pp_rank == self.pp_world - 1:
            x = self.norm(x)
            x = add_bias(self.output(x))
            return x
        else:
            return x


class PipelineStage(nn.Module):
    input_tensors: Optional[List[torch.Tensor]] = None

    def __init__(self, module):
        super().__init__()
        self.input_tensors = None
        self.wrapped = module

    def set_input_tensor(self, tensor: List[torch.Tensor]):
        self.input_tensors = tensor

    def forward(self, *x, **kwargs):
        if parallel_state.is_pipeline_first_stage():
            inputs = x
        else:
            inputs = self.input_tensors
        return self.wrapped(*inputs, **kwargs)


def model_provider_func(llama_args, *args, **kwargs):
    return PipelineStage(SplitLlama(llama_args, dtype=torch.bfloat16))


def loss_func(pred, label):
    label = rearrange(label, "b s -> s b").contiguous()
    loss = tensor_parallel.vocab_parallel_cross_entropy(pred, label).mean()
    averaged_loss = average_losses_across_data_parallel_group([loss])
    return loss, {"nice_loss": averaged_loss}


def train_forward_step_func(batch, model):
    input, label = batch
    out = model(input, start_pos=0)
    return out.contiguous(), lambda pred: loss_func(pred.float(), label)


def inference_forward_step_func(batch, model):
    (input,) = batch
    out = model(input, start_pos=0)
    return out.contiguous(), lambda pred: (pred, {"logits": pred})


# from apex
def set_random_seed(seed: int):
    """Set random seed for reproducability."""
    # Ensure that different pipeline MP stages get different seeds.
    # TP seeds are automatically offset by the TP rank by apex.

    seed = seed + (100 * parallel_state.get_pipeline_model_parallel_rank())
    import random
    import numpy as np

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    tensor_parallel.model_parallel_cuda_manual_seed(seed)


params = {
    65: ModelArgs(dim=8192, n_heads=64, n_layers=80, vocab_size=50432, norm_eps=1e-5),
    30: ModelArgs(
        dim=6656, n_heads=52, n_layers=60, vocab_size=50432, norm_eps=1e-6, max_seq_len=4096
    ),
    # 30: ModelArgs(dim=8192, n_heads=64, n_layers=36, vocab_size=50432, norm_eps=1e-6, max_seq_len=4096),
    15: ModelArgs(dim=8192, n_heads=64, n_layers=20, vocab_size=50432, norm_eps=1e-6),
    7: ModelArgs(dim=4096, n_heads=32, n_layers=32, vocab_size=50432, norm_eps=1e-6),
}


def convert_llama_state_dict(
    args: ModelArgs,
    state_dict,
    tp_rank: int,
    tp_world: int,
    pp_rank: int,
    pp_world: int,
):
    state_dict = state_dict.copy()
    state_dict.pop("rope.freqs")
    # in original code, token embeddings are sharded across latent dim, but apex shards them along vocab dim
    if pp_rank == 0:
        tok_embeds = state_dict["tok_embeddings.weight"].cuda()
        full_embeds = tensor_parallel.gather_from_tensor_model_parallel_region(tok_embeds)
        local_vocab_size = args.vocab_size // tp_world
        tok_embeds = full_embeds[tp_rank * local_vocab_size : (tp_rank + 1) * local_vocab_size]
        state_dict["tok_embeddings.weight"] = tok_embeds.cpu()
    else:
        state_dict.pop("tok_embeddings.weight")

    if pp_rank != (pp_world - 1):
        state_dict.pop("norm.weight")
        state_dict.pop("output.weight")

    def offset_layer_idx(name):
        stage_layers = args.n_layers // pp_world
        if name.startswith("layers."):
            layer_idx = int(name.split(".")[1])
            if pp_rank * stage_layers <= layer_idx < (pp_rank + 1) * stage_layers:
                new_layer_idx = layer_idx - pp_rank * stage_layers
                return name.replace(f"layers.{layer_idx}", f"layers.{new_layer_idx}")
            else:
                return None
        else:
            return name

    state_dict = {
        offset_layer_idx(k): v for k, v in state_dict.items() if offset_layer_idx(k) is not None
    }

    state_dict = {("module.wrapped." + k): v for k, v in state_dict.items()}
    return state_dict


from sentencepiece import SentencePieceProcessor
from logging import getLogger
from typing import List
import os


logger = getLogger()


class Tokenizer:
    def __init__(self, model_path: str):
        # reload tokenizer
        assert os.path.isfile(model_path), model_path
        self.sp_model = SentencePieceProcessor(model_file=model_path)
        logger.info(f"Reloaded SentencePiece model from {model_path}")

        # BOS / EOS token IDs
        self.n_words: int = self.sp_model.vocab_size()
        self.bos_id: int = self.sp_model.bos_id()
        self.eos_id: int = self.sp_model.eos_id()
        self.pad_id: int = self.sp_model.pad_id()
        logger.info(f"#words: {self.n_words} - BOS ID: {self.bos_id} - EOS ID: {self.eos_id}")
        assert self.sp_model.vocab_size() == self.sp_model.get_piece_size()

    def encode(self, s: str, bos: bool, eos: bool) -> List[int]:
        assert type(s) is str
        t = self.sp_model.encode(s)
        if bos:
            t = [self.bos_id] + t
        if eos:
            t = t + [self.eos_id]
        return t

    def decode(self, t: List[int]) -> str:
        return self.sp_model.decode(t)


def main():
    rank = int(os.environ["SLURM_PROCID"])
    world_size = int(os.environ["WORLD_SIZE"])
    gpus_per_node = int(os.environ["SLURM_GPUS_ON_NODE"])
    assert gpus_per_node == torch.cuda.device_count()
    print(f"hi from {rank}/{world_size} on {gethostname()}", flush=True)

    torch.distributed.init_process_group("nccl", rank=rank, world_size=world_size)

    local_rank = rank - gpus_per_node * (rank // gpus_per_node)
    torch.cuda.set_device(local_rank)

    tensor_model_parallel_size = 4
    pipeline_model_parallel_size = 1
    virtual_pipeline_model_parallel_size = None

    parallel_state.initialize_model_parallel(
        tensor_model_parallel_size,
        pipeline_model_parallel_size,
        virtual_pipeline_model_parallel_size,
    )

    world_size = torch.distributed.get_world_size()
    data_parallel_size: int = world_size // (
        tensor_model_parallel_size * pipeline_model_parallel_size
    )

    # tok = Tokenizer("/mnt/hdd/llama2/tokenizer.model")
    # llama_args = ModelArgs(**dict(params[65].__dict__, vocab_size=tok.n_words))
    llama_args = ModelArgs(**dict(params[30].__dict__, vocab_size=32000))

    tp_rank = parallel_state.get_tensor_model_parallel_rank()
    pp_rank = parallel_state.get_pipeline_model_parallel_rank()

    # state_dict = torch.load(f"/mnt/hdd/llama2/65B/consolidated.{tp_rank:02d}.pth")
    # state_dict = convert_llama_state_dict(
    #     llama_args,
    #     state_dict,
    #     tp_rank,
    #     tensor_model_parallel_size,
    #     pp_rank,
    #     pipeline_model_parallel_size,
    # )

    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True

    global_batch_size = 2048
    micro_batch_size = 1

    setup_microbatch_calculator(
        rank=rank,
        rampup_batch_size=None,
        global_batch_size=global_batch_size,
        micro_batch_size=micro_batch_size,
        data_parallel_size=data_parallel_size,
    )

    set_random_seed(2023)

    forward_backward_func = get_forward_backward_func(
        virtual_pipeline_model_parallel_size, pipeline_model_parallel_size
    )
    print(f"{forward_backward_func=}")

    model_kwargs = dict(llama_args=llama_args)
    wrap_with_ddp = True

    models = build_model(
        model_provider_func,
        wrap_with_ddp,
        virtual_pipeline_model_parallel_size,
        **model_kwargs,
    )

    # models[0].load_state_dict(state_dict)
    # print("loaded state dict", flush=True)

    local_rank = torch.cuda.current_device()

    # optimizer = torch.optim.AdamW(models[0].parameters(), lr=1e-4)

    optimizer = DistributedFusedAdam(
        models[0].parameters(),
        lr=1e-4,
        process_group=parallel_state.get_data_parallel_group(),
        dtype=torch.bfloat16,
        # distributed_process_group=torch.distributed.new_group(ranks=[torch.distributed.get_rank()]),
        # redundant_process_group=parallel_state.get_data_parallel_group(),
        store_params=False,
    )

    # optimizer = FusedAdam(models[0].parameters(), lr=1e-4)

    dp_rank = parallel_state.get_data_parallel_rank()

    data_loader = (
        torch.randint(
            0,
            llama_args.vocab_size,
            (100, global_batch_size, llama_args.max_seq_len + 1),
        )
        .long()
        .cuda()
    )

    data_loader = (
        torch.full(
            (100, global_batch_size, llama_args.max_seq_len + 1),
            fill_value=dp_rank,
        )
        .long()
        .cuda()
    )

    data_loader = (
        torch.arange(0, 100 * global_batch_size, dtype=torch.long)
        .repeat(llama_args.max_seq_len + 1)
        .reshape(100, global_batch_size, llama_args.max_seq_len + 1)
        .cuda()
        * 10
        + dp_rank
    )

    io_shape = (llama_args.max_seq_len, micro_batch_size, llama_args.dim)
    approx_model_flops = 8 * global_batch_size * llama_args.max_seq_len * 30e9

    if rank == 0:
        print(f"start {io_shape}", flush=True)

    # prompt = [tok.encode("Hello world, my name is", bos=True, eos=False)]
    # prompt_lengths = [len(p) for p in prompt]
    # prompt = [p + [tok.eos_id] * (len(p) - llama_args.max_seq_len) for p in prompt]
    # prompt = torch.tensor(prompt).long().cuda()

    # _reconfigure_microbatch_calculator(
    #     rank=rank,
    #     rampup_batch_size=None,
    #     global_batch_size=micro_batch_size,
    #     micro_batch_size=micro_batch_size,
    #     data_parallel_size=1,
    # )

    # with torch.no_grad():
    #     for i in range(100):
    #         output = forward_backward_func(
    #             inference_forward_step_func,
    #             [prompt],
    #             models,
    #             forward_only=True,
    #             tensor_shape=(prompt.shape[1], 1, llama_args.dim),
    #             dtype=torch.bfloat16,
    #         )

    #         if parallel_state.is_pipeline_last_stage():
    #             logits = output[0]["logits"].float()
    #             logits = rearrange(logits, "s b n -> b s n")
    #             logits = tensor_parallel.gather_from_tensor_model_parallel_region(
    #                 logits
    #             )
    #             prompt = torch.cat([prompt, logits[:, -1:].argmax(dim=-1)], dim=1)
    #             src = parallel_state.get_pipeline_model_parallel_last_rank()
    #             group = parallel_state.get_embedding_group()
    #             torch.distributed.broadcast(prompt, src, group)
    #         elif parallel_state.is_pipeline_first_stage():
    #             new_prompt = torch.empty(
    #                 (prompt.shape[0], prompt.shape[1] + 1),
    #                 dtype=prompt.dtype,
    #                 device=prompt.device,
    #             )
    #             src = parallel_state.get_pipeline_model_parallel_last_rank()
    #             group = parallel_state.get_embedding_group()
    #             torch.distributed.broadcast(new_prompt, src, group)
    #             prompt = new_prompt

    #         if rank == 0:
    #             text_output = tok.decode(prompt[0].cpu().numpy().tolist())
    #             print(text_output)

    # return
    # _reconfigure_microbatch_calculator(
    #     rank=rank,
    #     rampup_batch_size=None,
    #     global_batch_size=global_batch_size,
    #     micro_batch_size=micro_batch_size,
    #     data_parallel_size=data_parallel_size,
    # )

    for batch in data_loader:
        optimizer.zero_grad()
        inputs, labels = batch[:, :-1], batch[:, 1:]
        t = time.time()
        loss = forward_backward_func(
            train_forward_step_func,
            [inputs, labels],
            models,
            forward_only=False,
            tensor_shape=io_shape,
            dtype=torch.bfloat16,
            sync_batch_comm=False,
            sequence_parallel_enabled=True,
        )

        dt = time.time() - t
        if rank == (world_size - 1):
            print(f"tflops: {approx_model_flops / (dt * world_size) / 1e12=}", flush=True)
            memory_usage_gb = torch.cuda.max_memory_allocated() / 1e9
            print(f"memory usage: {memory_usage_gb=}", flush=True)
            samples_per_sec = global_batch_size / dt
            print(f"throughput: {samples_per_sec=}", flush=True)
            print(f"{len(loss)=}", flush=True)

        rmsnorms = [m for _, m in models[0].named_modules() if isinstance(m, RMSNorm)]
        rmsnorm_grads = [param.grad for rmsnorm in rmsnorms for param in rmsnorm.parameters()]
        rmsnorm_grads = [grad for grad in rmsnorm_grads if grad is not None]
        if rmsnorm_grads:
            coalesced = torch._utils._flatten_dense_tensors(rmsnorm_grads)
            torch.distributed.all_reduce(
                coalesced, group=parallel_state.get_tensor_model_parallel_group()
            )
            for buf, synced in zip(
                rmsnorm_grads, torch._utils._unflatten_dense_tensors(coalesced, rmsnorm_grads)
            ):
                buf.copy_(synced)

        optimizer.step()

    print("done", flush=True)


if __name__ == "__main__":
    main()
