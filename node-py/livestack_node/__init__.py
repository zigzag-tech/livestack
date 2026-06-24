"""livestack-node — the model-node kit.

The single-node + embedded-gateway case of Livestack: a plain web server becomes
a self-managing Livestack node whose GPU residence is driven entirely by leases
(resident iff leased-or-pinned), with no startup dependency on a remote gateway.

Ports:
  1. lease       — CapabilityLeaseStore (faithful port of capabilities.ts)
  2. facade      — build_router: the REST surface (optional fastapi dep)
  3. residence   — ResidenceController: lease lifecycle -> runtime warm/evict
  4. runtime     — ModelRuntime protocol (the only per-service adapter)
  client         — lease(): consumer context manager with graceful degradation
"""

from .lease import (
    Capability,
    CapabilityLeaseStore,
    Lease,
    Requirement,
    matches_requirement,
)
from .residence import ResidenceController
from .runtime import FakeRuntime, ModelRuntime
from .client import lease

__all__ = [
    "Capability",
    "CapabilityLeaseStore",
    "Lease",
    "Requirement",
    "matches_requirement",
    "ResidenceController",
    "ModelRuntime",
    "FakeRuntime",
    "lease",
]
