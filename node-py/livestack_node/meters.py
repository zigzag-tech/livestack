"""meters.py — best-effort live device-memory meters for the /residence snapshot.

A meter is a zero-arg callable returning::

    {"capacity": {"vram_bytes": <int>}, "free": {"vram_bytes": <int>}}

read off the REAL device, or ``None`` on any failure (the broker then falls back to
its configured budget). This is what lets the Harmony planner reconcile against
*measured* free memory instead of trusting static footprint bookkeeping — robust to
non-fleet processes, footprint drift, and (on Metal) unified-memory pressure.

Meters never raise: a node must keep serving even if memory introspection breaks.
"""
from __future__ import annotations

from typing import Callable, Optional


def cuda_meter(device: int = 0) -> Callable[[], Optional[dict]]:
    """Driver-level free/total via ``torch.cuda.mem_get_info`` — counts ALL processes
    on the GPU, not just this one, so it sees contention the fleet didn't declare."""
    def meter() -> Optional[dict]:
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            free, total = torch.cuda.mem_get_info(device)
            return {"capacity": {"vram_bytes": int(total)},
                    "free": {"vram_bytes": int(free)}}
        except Exception:
            return None
    return meter


def mlx_meter() -> Callable[[], Optional[dict]]:
    """Apple unified-memory view via MLX. ``capacity`` = the GPU working-set budget
    Apple recommends; ``free`` = budget minus live MLX allocations. Reclaimable cache
    is NOT counted as used (MLX frees it on demand), matching real allocatability."""
    def meter() -> Optional[dict]:
        try:
            import mlx.core as mx
            try:
                info = mx.device_info()
            except Exception:
                info = mx.metal.device_info()
            total = int(info.get("max_recommended_working_set_size")
                        or info.get("memory_size") or 0)
            if total <= 0:
                return None
            try:
                active = int(mx.get_active_memory())
            except Exception:
                active = int(mx.metal.get_active_memory())
            free = max(0, total - active)
            return {"capacity": {"vram_bytes": total},
                    "free": {"vram_bytes": free}}
        except Exception:
            return None
    return meter


def auto_meter() -> Optional[Callable[[], Optional[dict]]]:
    """Pick a meter by what's importable + available on this node: CUDA (torch) first,
    else MLX. ``None`` if neither — the broker falls back to the configured budget."""
    try:
        import torch
        if torch.cuda.is_available():
            return cuda_meter()
    except Exception:
        pass
    try:
        import mlx.core  # noqa: F401
        return mlx_meter()
    except Exception:
        pass
    return None
