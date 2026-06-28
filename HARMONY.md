# Harmony — GPU-residency arbitration for the fleet

**Harmony** is Livestack's priority-preemptive, lease-based GPU-residency layer. It
lets independent model servers — live ASR/TTS, interactive chip generation, batch
meeting-digest — **share one host's GPU (or a mesh of GPUs) by priority instead of
fighting over VRAM**. One model, resident once, serves unlimited concurrent leases;
when demand exceeds capacity, the least-important idle work yields to the most
important.

Harmony does not have its own repository — it lives inside `livestack`:

| Piece | Path | Role |
|---|---|---|
| **Residency core** | `shared/src/residency.rs` | Pure Rust per-process resident-set state machine + functional-health evict/reload. State-in/plan-out. |
| **Python binding** | `shared-py/` (`shared_py`) | pyo3 wrapper exposing the Rust core to Python. |
| **Executor shim** | `polycore/` (`polycore`) | Thin per-process side-effects shim (load/unload/probe/gc) over the Rust core. The `ModelManager`/`Coordinator` seam each server implements. |
| **Node kit** | `node-py/livestack_node/` | The lease-driven `Coordinator`, the `/livestack` REST facade, the `attach()` one-liner, and the live device meters. |
| **Placement planner** | `node-py/livestack_node/planner.py` | The pure cross-process **brain**: `WorldState → Plan` (load/evict/grant/defer). |
| **Broker daemon** | `node-py/livestack_node/hostd.py` + `hostbroker.py` | The **authority** that runs the planner across processes on a host and dispatches actions. `python -m livestack_node.hostd`. |

## Two brains, two layers

- **`planner.py` — placement** (cross-process): given the whole world (every unit's
  footprint, priority, residency tier; what's resident/busy; measured free memory),
  decide *what should be resident where*. Pure function, fully unit-tested, no I/O.
- **`residency.rs` / `polycore` — execution** (per-process): inside one server,
  own the resident set, the idle clock, and functional-health reload. Carries out
  warm/evict on the GPU thread.

The broker is the thin layer that turns one into the other across the separate
`polyasr` / `polytts` / `chipgen` processes (an in-process coordinator can only move
its own units).

## Residency tiers & priority

Residency tier (per unit, mirrors `polycore.ResidencyPolicy`):

- **HARD_PIN** — kept warm, never preempted, never the last replica evicted (ASR).
- **SOFT_PIN** — preferred-warm but preemptible under pressure; restored with
  hysteresis once pressure settles (TTS / voxcpm).
- **UNPINNED** — pure demand residence; first evicted, last restored (chipgen).

Priority is **decoupled** from the tier (lower = more important). Default fleet
priorities (`hostd.DEFAULT_PRIORITIES`):

```
asr 10  <  align/diarize 15  <  qwen/voxcpm 20  <  chipgen 30
```

So a meeting-digest `align` (demand-driven, UNPINNED residence, but priority 15) can
preempt idle TTS or chipgen.

Two anti-pathology guards: **anti-thrash** (a freshly-loaded unit is protected by
`min_residency_s` before it may be preempted; a preempted SOFT_PIN waits
`restore_debounce_s` before restore) and **anti-starvation** (a deferred request's
effective priority ages upward so low-priority work is never starved forever).

## Context-awareness: it plans against *measured* reality

Two things keep the plan tied to the real machine, not just declared estimates:

1. **Live device meters** (`meters.py`). Each node reports real memory in its
   `/residence` snapshot: `cuda_meter` uses `torch.cuda.mem_get_info` (driver-level
   free/total, counts **all** processes); `mlx_meter` uses `mx.device_info()` working
   set minus live MLX allocations (Apple unified memory). `attach(device_meter="auto")`
   picks one by backend, so a node becomes memory-aware on redeploy with no
   server-side change. The planner reconciles measured free against the configured
   budget and uses the **tighter** of the two — so an external process, a
   bigger-than-declared model, or an activation spike the static footprints miss
   cannot cause an OOM grant. When real free goes negative vs. the static model, the
   planner **sheds** idle, non-pinned, least-important units to relieve the pressure
   (`planner.plan` step 0).

2. **Proactive reconcile loop** (`hostd`, `LIVESTACK_REPLAN_INTERVAL`, default 5 s).
   Planning is otherwise pull-based (it runs at each `/admit` — synchronously, *before*
   a load, so room is made first). The reconcile loop additionally re-snapshots and
   re-plans with no pending request, so Harmony reacts *between* admissions:
   re-asserts the HARD_PIN floor, restores debounced SOFT_PINs, and sheds under
   measured pressure.

## How a server joins (zero ceremony)

```python
from livestack_node import attach
manager, coordinator = attach(
    app, host_id="zz-tower0", kind="polyasr", units=units,
    idle_seconds=180, coload=True, gpu_call=gpu_call)   # device_meter="auto"
```

`coload=True` (ASR's co-resident units) vs `False` (one-model-in-VRAM TTS engines) and
which unit is HARD_PIN are the only things that differ between servers.

## Endpoints

Broker (`hostd`, default `:8799`):

- `POST /admit {"kind": "align"}` → `{granted, device_id, plan}` — make room *before* a load.
- `GET /status` → per-node residence snapshot + last-evicted bookkeeping.
- `GET /plan` → dry-run desired plan, no dispatch.

Node (`/livestack` facade): `GET /capability`, `GET /health`, `GET /residence`
(now includes `device_mem`), `POST /lease`, `POST /lease/{id}/heartbeat`,
`POST /lease/{id}/release`, `POST /model/warm`, `POST /model/evict`.

A batch consumer (e.g. the meeting-digest pipeline) is a good Harmony citizen: it
`/admit`s, holds residence leases on the units it needs for the run (heartbeated,
released in `finally`), and keeps non-GPU work (LLM digest, embeddings) off the card.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `LIVESTACK_PEERS` | localhost polyasr/polytts/chipgen | comma-separated `/livestack` base URLs |
| `LIVESTACK_HOST_ID` | `zz-tower0` | this host's id |
| `LIVESTACK_VRAM_GB` | `24` | per-device budget (policy ceiling) |
| `LIVESTACK_RESERVED_GB` | `2` | activation/driver slack never allocated |
| `LIVESTACK_REPLAN_INTERVAL` | `5` | reconcile-loop period (s); `0` disables |
| `LIVESTACK_DEVICES` | — | JSON per-device capacity for multi-host federation |
| `LIVESTACK_BROKER_PORT` | `8799` | broker HTTP port |

## Where it runs

- **zz-tower0** (RTX 3090, 24 GB): `livestack-hostd.service` brokers `polyasr` +
  `chipgen` (+ `polytts` where present). CUDA meter.
- **xc-mac-studio** (Apple Silicon, 36 GB unified): `io.zigzag.livestack-hostd`
  brokers `polyasr` (MLX) + `polytts` (MLX). MLX meter.

## Tests

```
cd node-py && python -m pytest -q          # planner, broker, facade, federation, lease
cargo test -p livestack-shared             # the Rust residency core
```
