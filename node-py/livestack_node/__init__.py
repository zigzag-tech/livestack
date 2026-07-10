"""livestack-node — Harmony's per-process node kit AND executor.

Everything is managed via livestack Harmony now: this one package owns both the
per-process **executor** (``ModelManager``/``ManagedUnit``/``ResidencyPolicy``, the
``Coordinator`` seam, the backend freeing callbacks — formerly the separate
``polycore`` package, now retired into here) and the cross-process node kit:

  - ModelManager/ManagedUnit/ResidencyPolicy : the per-process executor (load/unload/
                           run-with-measurement/idle-evict) over the Rust planner.
  - Coordinator / LocalCoordinator            : the residency policy seam + its
                           in-process default.
  - LivestackCoordinator : lease-driven Coordinator (resident iff leased-or-pinned).
  - attach()             : the one call polyasr & polytts make, identically, to
                           build a manager + coordinator + REST facade.
  - build_router         : the uniform /livestack REST surface.
  - CapabilityLeaseStore : the lease broker (also federatable).
  - lease()              : consumer-side lease context manager.

Pinning is expressed on the units via ResidencyPolicy.HARD_PIN.
"""
from .lease import (
    Capability,
    CapabilityLeaseStore,
    Lease,
    Requirement,
    matches_requirement,
)
from .manager import ManagedUnit, ModelManager, ResidencyPolicy
from .coordinator import Coordinator, LocalCoordinator, LivestackCoordinator
from .freeing import free_cuda, free_mlx, trim_ram, noop_free
from .facade import build_router
from .serve import attach
from .client import lease
from .measure import measure_footprint
from .hostbroker import HostBroker, Peer, RestPeer
from .planner import (
    plan, Plan, PlannerPolicy, WorldState, Device, Unit, Placement, Request,
    Residency, Load, Evict, Grant, Defer,
)
from .provision import (
    Provisioner, ComputeSpec, ComputeHandle, Offer, Instance,
    ProvisionError, CapacityError, leased, reap_orphans,
)
from .fleet_dispatch import dispatch, run_on_provider

__all__ = [
    "ManagedUnit", "ModelManager", "ResidencyPolicy",
    "Coordinator", "LocalCoordinator",
    "free_cuda", "free_mlx", "trim_ram", "noop_free",
    "LivestackCoordinator",
    "attach",
    "build_router",
    "Capability",
    "CapabilityLeaseStore",
    "Lease",
    "Requirement",
    "matches_requirement",
    "lease",
    "plan", "Plan", "PlannerPolicy", "WorldState", "Device", "Unit",
    "Placement", "Request", "Residency", "Load", "Evict", "Grant", "Defer",
    "HostBroker", "Peer", "RestPeer", "measure_footprint",
    "Provisioner", "ComputeSpec", "ComputeHandle", "Offer", "Instance",
    "ProvisionError", "CapacityError", "leased", "reap_orphans",
    "dispatch", "run_on_provider",
]
