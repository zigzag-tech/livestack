# livestack-node (Python)

When several of these nodes share one host's GPU, the multi-node arbitration
layer that plans residence across them by priority is **Harmony** (the
`hostd` broker, `python -m livestack_node.hostd`). This package is the
per-node half of that system.

The **model-node kit**: turn a plain GPU web server into a self-managing
Livestack node whose VRAM residence is driven entirely by leases —

> **a unit is resident iff it holds ≥1 active lease, or is pinned.**

This is the single-node + embedded-gateway case of Livestack (see
`../_plans/restful-model-node.md`). The server needs **no startup connection to a
remote gateway**: the lease store is an in-process object. When a host/mesh
gateway later appears, federation is inbound; absent it, the node keeps running.

## Ports

| Port | Module | Generic? | Role |
|---|---|---|---|
| 1 Lease plane | `lease.py` | ✅ | `CapabilityLeaseStore` — faithful port of `core/src/capabilities.ts` |
| 2 REST facade | `facade.py` | ✅ | `build_router(manager, coordinator, capability, gpu_call, ...)` → mountable FastAPI router |
| 3 Residence | `coordinator.py` | ✅ | `LivestackCoordinator` — lease lifecycle → manager load/evict |
| 4 Runtime | `manager.py` + `freeing.py` | ✅ core; units per-service | `ModelManager`/`ManagedUnit`/`ResidencyPolicy` — side-effects over the Rust planner |
| Consumer | `client.py` | ✅ | `lease()` context manager with graceful no-op degradation |

`reap_expired` alone only deletes lease records; **port 3 is the bridge that
actually frees VRAM** — `LivestackCoordinator.idle_sweep()` reaps, then evicts
whatever is neither leased nor pinned.

The one-call wiring of ports 2–4 is `attach()` (`serve.py`); polyasr and
polytts both call it identically.

## The Harmony broker layer also lives here

Besides the per-node kit, the package carries Harmony's broker/planner side:
`hostd.py`/`hostbroker.py` (the broker daemon and its peers), `membership.py`,
`announce.py` (node self-registration), `planner.py` (cross-node placement),
`meters.py`/`measure.py` (device metering, activation measurement),
`provision.py`/`provision_runpod.py` (rent-a-box), and
`fleet_dispatch.py`/`fleet_scheduler.py`. See `../HARMONY.md` for that layer —
this README covers the per-node half.

## How the old mechanisms collapse into leases

| Bespoke (today) | Lease semantics |
|---|---|
| `IDLE_EVICT_SECONDS=180` idle timer | TTL usage lease, auto-renewed on use |
| `touch()` per WS frame | lease heartbeat |
| `IDLE_EVICT_SECONDS=0` (pin) | permanent residence (`ResidencyPolicy.HARD_PIN` on the unit) |
| `POST /model/unload` | `release()` the lease |

## Wiring a server (e.g. polyasr)

```python
from livestack_node import ManagedUnit, ResidencyPolicy, attach, free_cuda

units = {
    "asr":     ManagedUnit("asr", load_asr, free_cuda,
                           residency_policy=ResidencyPolicy.HARD_PIN),  # hot model stays resident
    "align":   ManagedUnit("align", load_align, free_cuda),
    "diarize": ManagedUnit("diarize", load_diarize, free_cuda),
}

manager, coordinator = attach(app, host_id="zz-tower0", kind="polyasr",
                              units=units, idle_seconds=180, coload=True,
                              gpu_call=gpu_call)   # runs a thunk under the server's GPU lock
```

`attach()` builds the `LivestackCoordinator` and `ModelManager` and mounts the
facade router at `/livestack`; it returns both. Pinning is
`ResidencyPolicy.HARD_PIN` per unit; the idle-evict analog is
`coordinator.idle_sweep()`, driven by `manager.maybe_evict()`. `gpu_call` is
required — warm/evict must never race in-flight GPU work.

## Consumer (e.g. media-corpus pipeline)

```python
from livestack_node import lease
with lease("diarize", base_url="http://127.0.0.1:8766/livestack"):
    requests.post(f"{POLYASR}/v1/diarize", ...)   # warm + protected; no-op if unsupported
```

## Tests

```
cd node-py && python3 -m pytest -q
```

The REST contract (`tests/test_facade.py`) is the cross-language conformance
boundary — the same vectors should hold against the canonical TS core.
