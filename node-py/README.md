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
| 2 REST facade | `facade.py` | ✅ | `build_router(controller, capability)` → mountable FastAPI router |
| 3 Residence | `residence.py` | ✅ | `ResidenceController` — lease lifecycle → runtime warm/evict |
| 4 Runtime | `runtime.py` | ❌ per-service | `ModelRuntime` protocol (`warm`/`evict`/`resident`, **per-unit**) |
| Consumer | `client.py` | ✅ | `lease()` context manager with graceful no-op degradation |

`reap_expired` alone only deletes lease records; **port 3 is the bridge that
actually frees VRAM.**

## How the old mechanisms collapse into leases

| Bespoke (today) | Lease semantics |
|---|---|
| `IDLE_EVICT_SECONDS=180` idle timer | TTL lease, auto-renewed on use |
| `touch()` per WS frame | lease heartbeat |
| `IDLE_EVICT_SECONDS=0` (pin) | permanent residence (`pin`) |
| `POST /model/unload` | `release()` the lease |

## Wiring a server (e.g. polyasr)

```python
from livestack_node import Capability, ResidenceController
from livestack_node.facade import build_router

class AsrRuntime:                     # port 4: wraps AsrModelManager (per-unit!)
    def warm(self, unit): ...
    def evict(self, unit): ...         # MUST affect only `unit`
    def resident(self): ...

units = {k: Capability(kind=k, host_id="zz-tower0", lease_ttl_seconds=180)
         for k in ("asr", "align", "diarize")}
controller = ResidenceController("zz-tower0", AsrRuntime(), units)
controller.pin("asr")                  # benchday's hot model stays resident
controller.start_sweep(10.0)           # idle-evict analog
app.include_router(build_router(controller, Capability(kind="polyasr", host_id="zz-tower0")),
                   prefix="/livestack")
```

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
