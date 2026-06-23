"""Backend-specific GPU/heap freeing callbacks. Zero hard dependencies — every
backend import is lazy and best-effort so polycore stays installable anywhere.

Lifted verbatim from polyasr_manager / polytts so behaviour is identical; this is
the one place that knows CUDA vs MLX vs libc."""
from __future__ import annotations


def free_cuda() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
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
