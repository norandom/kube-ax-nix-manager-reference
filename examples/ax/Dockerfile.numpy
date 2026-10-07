# Workflow image for numpy/pandas jobs on AX. Must stay based on the task runner: every AX task
# image needs /usr/local/bin/ax-task-runner. Make the pushed package public on ghcr.
FROM ghcr.io/norandom/ax-task-runner:v0.3.1
RUN pip install --no-cache-dir numpy pandas pyarrow
# Thread pools sized by the job (see docs/workloads.md); one BLAS thread per requested CPU.
ENV OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 NUMEXPR_NUM_THREADS=2 \
    MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1
COPY job.py /opt/job/job.py
