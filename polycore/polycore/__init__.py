"""DEPRECATED transition shim — polycore has been retired into livestack_node.

Everything is managed via livestack Harmony now: the per-process executor
(ModelManager/ManagedUnit/ResidencyPolicy), the Coordinator seam, and the freeing
callbacks all live in ``livestack_node``. This module re-exports them ONLY so a
not-yet-migrated ``from polycore import X`` keeps working during rollout. It is
deleted once every consumer imports from ``livestack_node`` directly.
"""
from livestack_node.manager import ManagedUnit, ModelManager, ResidencyPolicy
from livestack_node.coordinator import Coordinator, LocalCoordinator
from livestack_node.freeing import free_cuda, free_mlx, trim_ram, noop_free

__all__ = [
    "ManagedUnit", "ModelManager", "ResidencyPolicy",
    "Coordinator", "LocalCoordinator",
    "free_cuda", "free_mlx", "trim_ram", "noop_free",
]
