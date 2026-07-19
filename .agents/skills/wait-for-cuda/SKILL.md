---
name: wait-for-cuda
description: Keep GPU-dependent tasks running through temporary CUDA unavailability by probing GPU state, waiting 10 seconds, and retrying the failed step in a fresh process. Use when a task reports that CUDA is unavailable, a GPU is busy or temporarily inaccessible, CUDA initialization fails, no GPU is momentarily visible, or another process may be occupying GPU resources; do not immediately abandon the current task for these transient failures.
---

# Wait for CUDA

Treat temporary CUDA unavailability as a recoverable resource condition. Preserve completed work and retry the smallest failed GPU-dependent step instead of exiting the task or restarting the entire workflow.

## Classify the failure

1. Capture the failed command and its complete error output.
2. Treat messages such as `CUDA unavailable`, `all CUDA-capable devices are busy or unavailable`, temporary device discovery failures, and CUDA or NVML initialization failures as potentially transient.
3. For CUDA out-of-memory errors, inspect GPU use first. Treat the error as transient only when another process appears to be consuming the required memory; otherwise diagnose the workload's memory demand instead of blindly retrying it.
4. Do not apply this retry loop to deterministic failures such as invalid device indices, incompatible CUDA or driver versions, missing libraries, compilation errors, invalid arguments, or reproducible application bugs.

## Retry the failed step

1. Record any output, checkpoint, or progress already produced by the task.
2. Probe GPU availability with `nvidia-smi` when available. Treat a failed probe as additional evidence of temporary unavailability, not as an immediate reason to exit.
3. Wait 10 seconds.
4. Re-probe the GPU, then rerun only the failed GPU-dependent command in a fresh process. Do not reuse a process whose CUDA context failed to initialize.
5. Repeat the wait and retry cycle up to 15 times by default.
6. Reset the retry count after the failed step succeeds, and continue the original task from the preserved progress.

Keep the user informed with short status updates while waiting, including the current attempt count and the observed GPU state. Continue other independent, non-GPU work during the wait when it is safe and useful.

## Escalate safely

- Do not kill or interrupt other users' GPU processes.
- Do not silently switch devices, change `CUDA_VISIBLE_DEVICES`, reduce model or batch settings, or fall back to CPU unless the task already permits it or the user authorizes the change.
- Do not rerun earlier steps that may have side effects when only the GPU step failed.
- After 15 unsuccessful retries, perform one final probe and report the exact error, retry history, and current GPU state. Stop only the blocked GPU step; preserve artifacts and clearly identify how the original task can resume.
- Extend the bounded retry window only when current evidence shows that a known competing process is likely to release the GPU soon. Continue reporting progress and never wait indefinitely without a clear limit.
