"""Example shard of a fan-out workflow: each task handles slice TASK_INDEX of TASK_COUNT."""

import os
import socket
import time

import numpy as np

index, count = int(os.environ.get("TASK_INDEX", 0)), int(os.environ.get("TASK_COUNT", 1))
rng = np.random.default_rng(index)
start = time.perf_counter()
a = rng.standard_normal((2048, 2048), dtype=np.float32)
for _ in range(10):
    a = (a @ a.T) / 2048.0
elapsed = time.perf_counter() - start
threads = os.environ.get("OPENBLAS_NUM_THREADS")
print(
    f"shard {index}/{count} on {socket.gethostname()}: checksum={float(a.sum()):.4f} "
    f"in {elapsed:.1f}s, BLAS threads={threads}"
)
