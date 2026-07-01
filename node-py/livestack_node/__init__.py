"""livestack-node — the livestack side of the polycore residency seam.

polycore (zero-dep, dependency-inverted) owns the per-process executor
(ModelManager) and the Coordinator Protocol. This package ships:

  - LivestackCoordinator : lease-driven implementation of polycore.Coordinator
                           (resident iff leased-or-pinned; per-unit idle evict).
  - attach()             : the one call polyasr & polytts make, identically, to
                           build a polycore manager + coordinator + REST facade.
  - build_router         : the uniform /livestack REST surface.
  - CapabilityLeaseStore : the lease broker (also federatable).
  - lease()              : consumer-side lease context manager.

Pinning is expressed on the units via polycore.ResidencyPolicy.HARD_PIN.
"""
from .lease import (
    Capability,
    CapabilityLeaseStore,
    Lease,
    Requirement,
    matches_requirement,
)
from .coordinator import LivestackCoordinator
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
