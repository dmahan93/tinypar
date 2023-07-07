import json
import numpy as np
import matplotlib.pyplot as plt

with open("data_check.json") as f:
    data = json.load(f)
diff = np.array([np.array(item[1][:-1]) - np.array(item[0][1:]) for item in data])
diff[diff != 0] = 1
plt.plot(diff[12])
plt.show()