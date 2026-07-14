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
# --- eager: the pure-stdlib layers --------------------------------------------
#
# PROVISIONING AND LEASING MUST IMPORT WITHOUT THE RUST CORE. They are pure stdlib
# and have nothing to do with GPU residency. Importing the residency executor here
# eagerly meant that anyone who only wanted to rent a box — `from
# livestack_node.provision import ComputeSpec` — got an ImportError demanding they
# build a Rust residency planner with maturin. That broke every provisioning
# consumer on any machine without the compiled core, including Benchday's weekly
# LoRA retrain, which does not do residency at all.
from .lease import (
    Capability,
    CapabilityLeaseStore,
    Lease,
    Requirement,
    matches_requirement,
)
from .provision import (
    Provisioner, ComputeSpec, ComputeHandle, Offer, Instance,
    ProvisionError, CapacityError, leased, reap_orphans,
)
from .fleet_dispatch import dispatch, run_on_provider

# --- lazy: the residency executor (needs the compiled Rust planner) ------------
#
# PEP 562. These resolve on first attribute access, so `livestack_node.ModelManager`
# still works exactly as before and still raises the same actionable "build it with
# maturin" error — but only for callers who actually want residency.
_LAZY = {
    "ManagedUnit": ".manager", "ModelManager": ".manager",
    "ResidencyPolicy": ".manager",
    "Coordinator": ".coordinator", "LocalCoordinator": ".coordinator",
    "LivestackCoordinator": ".coordinator",
    "free_cuda": ".freeing", "free_mlx": ".freeing", "trim_ram": ".freeing",
    "noop_free": ".freeing",
    "build_router": ".facade",
    "attach": ".serve",
    "lease": ".client",
    "measure_footprint": ".measure",
    "HostBroker": ".hostbroker", "Peer": ".hostbroker", "RestPeer": ".hostbroker",
    "plan": ".planner", "Plan": ".planner", "PlannerPolicy": ".planner",
    "WorldState": ".planner", "Device": ".planner", "Unit": ".planner",
    "Placement": ".planner", "Request": ".planner", "Residency": ".planner",
    "Load": ".planner", "Evict": ".planner", "Grant": ".planner",
    "Defer": ".planner",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod = importlib.import_module(module, __name__)
    value = getattr(mod, name)
    globals()[name] = value       # cache; subsequent access is a plain lookup
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))

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
