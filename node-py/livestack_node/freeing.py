"""Backend-specific GPU/heap freeing callbacks. Zero hard dependencies — every
backend import is lazy and best-effort so this stays installable anywhere.

Lifted verbatim from polyasr_manager / polytts so behaviour is identical; this is
the one place that knows CUDA vs MLX vs libc."""
from __future__ import annotations


def free_cuda() -> None:
    """Return this process's cached CUDA memory to the driver.

    `empty_cache()` alone is not enough, and the gap cost a card: it can only
    release a segment with NO live block in it, and PyTorch allocates cuBLAS
    workspaces THROUGH the caching allocator, per (device, stream), referenced
    by no Python tensor. Measured on xc-tower-ubuntu 2026-09-08, on a polyasr
    node that had served for a few minutes and then evicted every unit:

        reserved 3456.1 MB   allocated 9.6 MB   segments 2
        after empty_cache()          -> unchanged, 0 bytes returned
        after clearing workspaces    -> allocated 0.0 MB
        then empty_cache()           -> reserved 0.0 MB, all 3.4 GB returned

    9.6 MB of workspace pinned 3.4 GB, `/model/reclaim` reported `freed=0.0GB`
    265 times, and the leak Harmony correctly detected had no working lever.

    Synchronize FIRST: the workspaces are freed back to the allocator, so any
    kernel still using one must have finished. This runs on the GPU executor
    (nothing else in the process is touching the device) — do not call it from
    a thread that does not hold that lock.
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return
        torch.cuda.synchronize()
        try:
            torch._C._cuda_clearCublasWorkspaces()
        except AttributeError:
            pass          # older torch: nothing to clear, empty_cache is all we have
        torch.cuda.empty_cache()
    except Exception:
        pass


def free_mlx() -> None:
    try:
        import mlx.core as mx
        mx.clear_cache()
    except Exception:
        pass


def trim_ram() -> None:
    """Return freed heap pages to the OS so a co-resident workload doesn't get
    OOM-killed while we sit idle with no model loaded."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def noop_free() -> None:
    """For fakes/tests and CPU units that hold nothing on a device."""
    return None
