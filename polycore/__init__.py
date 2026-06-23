"""polycore — zero-dependency model-residency seam shared by polyasr & polytts.

Public API:
    ManagedUnit, ModelManager, ResidencyPolicy   — the local executor
    Coordinator, LocalCoordinator                 — the policy seam
    free_cuda, free_mlx, trim_ram, noop_free      — backend freeing callbacks

livestack ships a LivestackCoordinator implementing Coordinator; it is never
imported here (dependency inversion).
"""
from .freeing import free_cuda, free_mlx, trim_ram, noop_free
from .manager import ManagedUnit, ModelManager, ResidencyPolicy
from .coordinator import Coordinator, LocalCoordinator

__all__ = [
    "ManagedUnit", "ModelManager", "ResidencyPolicy",
    "Coordinator", "LocalCoordinator",
    "free_cuda", "free_mlx", "trim_ram", "noop_free",
]
