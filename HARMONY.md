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
| **Executor shim** | `node-py/livestack_node/` (`manager.py`, `coordinator.py`, `freeing.py`) | Thin per-process side-effects shim (load/unload/probe/gc) over the Rust core — `ModelManager` + `ResidencyPolicy` in `manager.py`. The `ModelManager`/`Coordinator` seam each server implements. (Retired `polycore/`.) |
| **Node kit** | `node-py/livestack_node/` | The lease-driven `Coordinator`, the `/livestack` REST facade, the `attach()` one-liner, and the live device meters. |
| **Placement planner** | `node-py/livestack_node/planner.py` | The pure cross-process **brain**: `WorldState → Plan` (load/evict/grant/defer). |
| **Broker daemon** | `node-py/livestack_node/hostd.py` + `hostbroker.py` | The **authority** that runs the planner across processes on a host and dispatches actions. `python -m livestack_node.hostd`. |
| **Membership** | `node-py/livestack_node/membership.py` + `announce.py` | Who is on this host, and who has gone. Pure roster/state machine + the node-side registrar. |

`livestack_node/` also carries fleet modules beyond this doc's scope — `client.py`,
`fleet_dispatch.py`, `fleet_scheduler.py`, `provision.py`, `provision_runpod.py`,
`measure.py` (tests `test_fleet_*.py`, `test_provision.py`).

## Two brains, two layers

- **`planner.py` — placement** (cross-process): given the whole world (every unit's
  footprint, priority, residency tier; what's resident/busy; measured free memory),
  decide *what should be resident where*. Pure function, fully unit-tested, no I/O.
- **`residency.rs` / `livestack_node` — execution** (per-process): inside one server,
  own the resident set, the idle clock, and functional-health reload. Carries out
  warm/evict on the GPU thread.

The broker is the thin layer that turns one into the other across the separate
`polyasr` / `polytts` / `chipgen` processes (an in-process coordinator can only move
its own units).

## Membership: starting a node is the only action required

A node **reports for duty**. On startup, and every 30 s after, `attach()` POSTs
its facade URL to the broker (`LIVESTACK_BROKER_URL`, default
`http://127.0.0.1:8799`) and the broker snapshots it from there. There is
nothing to configure: the node knows the port it is about to serve, and it
retries until the broker answers, so start order does not matter and a broker
restart refills from the nodes within one interval.

`LIVESTACK_PEERS` still works, demoted to **seeds** for nodes too old to
announce themselves, and the localhost defaults (`8766/8100/8844`) stay as
seeds too. They are guesses — this fleet's polyasr serves 8765, not 8766 — but
a wrong guess is now *visible* (`mia` in `GET /peers`, with its connect error)
and *cheap* (probed on a backoff, not every cycle), which is exactly what it
was not before. Removing them would silently empty the roster of every existing
deployment on upgrade, until its nodes were upgraded too.

The broker ages each peer from its last **success** — `fresh` → `suspect` (45 s)
→ `mia` (10 min) — and probes absent peers on a backoff instead of every cycle.
Any single success restores `fresh` immediately: membership is a report, not a
promise. Transitions are logged; "still gone" is not an event. Before this, a
node that was down produced one identical log line per reconcile tick — 92,089
of them and 9.2 MB on one host — with no way to tell three weeks of absence from
three seconds.

A **registered** peer is pruned after a bounded absence (it will re-announce);
a **seeded** peer never is, because an operator writing it down said it ought to
exist. `GET /peers` reports state, how long each has been unseen, and the last
probe error.

`GET /livestack/capability` is the node's **readiness descriptor** — the stable
thing a consumer reads instead of scraping `/health`. `ready` means the model is
resident and the server's own functional probe passes; a probe that throws
reports not-ready rather than falling back to a generic claim of fitness.

Design record: `_plans/peer-membership.md`.

## Residency tiers & priority

Residency tier (per unit, mirrors `livestack_node.manager.ResidencyPolicy`):

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

## Build hosts — the first *peerless* consumer (lodestar image builds)

The lodestar deploy tooling (`scripts/remote-build.mjs` in lodestar-platform) needs to pick
**which machine runs each `docker build`** — the same "where does this unit of work go"
question Harmony answers for GPUs, over machines that run **no livestack node at all**.
That peerless case needed three additions, all generic:

1. **Config-declared units** (`LIVESTACK_UNITS`, `HostBroker(extra_units=)`). Units used to
   come only from peers, so `admit("build")` on a node-less broker deferred "unknown kind".
   Config units are merged in `snapshot()` with `setdefault` — peers stay authoritative.
2. **A broker-held lease ledger.** `_hosted_has_room` always saw an empty house for hosted
   devices because *placements* also came only from peers. Now a grant on a hosted device
   checks out a lease (`/admit` returns `lease_id`), the client heartbeats it
   (`POST /lease/{id}/heartbeat`) and releases it (`POST /lease/{id}/release`), and the
   broker expires leases past `LIVESTACK_LEASE_TTL_S` (default 120 s) inside `snapshot()` —
   a dead leaseholder must not hold capacity. The ledger is the *only* place the broker is
   authoritative over a client; it is deliberately soft state (a broker restart briefly
   over-admits; leases re-check out).
3. **A health prober** (`LIVESTACK_PROBES`): per-device shell commands run on the reconcile
   loop — exit 0 keeps the device a candidate, anything else gates it (`available=False`)
   and demand fails over to the next arch-matching host. `POST /devices/{id}/health` is the
   same gate set by hand; `/status` shows both under `"hosted"`.

A **build broker** is then just a peerless `hostd` (`LIVESTACK_PEERS=none` — the explicit
opt-out from the localhost GPU seeds). The live one runs on zz-tower2 as systemd unit
`livestack-buildd` (port 8800, env in `~/livestack/node-py/buildd.env`, log
`~/livestack/node-py/buildd.log`), declaring each lodestar build host as a hosted device
whose id equals its key in lodestar's `deploy/build-hosts.json`:

```
LIVESTACK_UNITS={"build": {"priority": 20}}
LIVESTACK_DEVICES={"zz-tower2": {"hosted": true, "concurrency": 1, "cost_bias": 0,
                                  "labels": {"arch": "linux/amd64"}},
                   "xc-win-1":  {"hosted": true, "concurrency": 1, "cost_bias": 2,
                                  "labels": {"arch": "linux/amd64"}}}
```

`cost_bias` encodes *preference*, same knob as the vendor endpoints: zz-tower2 (native
amd64, datacenter ship path) is the default; xc-win-1 is next; a slow cross-arch host would
carry a bigger bias as overflow-only. `selector={"arch": "linux/amd64"}` on the request is
the arch match — the planner's label selector, nothing new. The probes are the same checks
`remote-build.mjs` preflights (ssh + docker + repo + disk). The client contract: **broker
down, deferred, or granting an unknown host ⇒ the client falls back to its static default
host** — a prod deploy is never blocked by this broker.

## Hosted backends — arbitrating somebody else's GPU

A **hosted backend** is a vendor endpoint (Qwen3-ASR on Alibaba Model Studio, a
managed STT API) declared as a `Device` with `hosted=True`. It has **no
residency**: nothing loads, nothing evicts, nothing idles out, and it can never
be a preemption victim or satisfy a HARD_PIN floor — a pin means "a warm local
replica", and a hosted device holds nothing.

What it does have is a **concurrency ceiling**, a **price**, and an **uptime**,
so it is scheduled by lease count rather than by bytes:

| Field | Meaning |
|---|---|
| `hosted` | marks the device as having no residency |
| `capacity["concurrency"]` | max in-flight leases; absent means unmetered |
| `cost_bias` | added to every placement option on this device |
| `available` | health gate; an unavailable backend is not a candidate |

`cost_bias` is the whole policy, in one number:

- **negative** — the hosted backend is the **default**. It beats even a warm
  local replica (cost 0), which is how interactive ASR moves off the card and
  leaves it for `align`/`diarize`/`chipgen`. That is a capacity win, not just a
  vendor swap: the contention Harmony was built to arbitrate largely disappears
  when the interactive tier stops needing VRAM at all.
- **positive** — **overflow only**. The GPU is preferred; the endpoint absorbs
  what would otherwise force a load, a preemption, or a `Defer`.

Declared in `LIVESTACK_DEVICES` (hosted rows carry no vram and are never
discovered, since nothing on the host reports them):

```json
{"qwen-sg": {"hosted": true, "concurrency": 8, "cost_bias": -1,
             "labels": {"region": "apac-sg"}}}
```

Health is set through `broker.hosted_available[device_id]`. A rate-limited or
down endpoint simply stops being offered and demand lands on the GPU — there is
no other special case anywhere in the planner.

**What it does not decide.** Harmony arbitrates work running *on a host*. A
client far from the host should call the vendor directly rather than round-trip
through a broker to be told to — routing a phone's audio through Nanjing to
reach Singapore is slower than either leg. Harmony's job is the fleet's use of
the endpoint (batch ingest, chipgen), and the shared policy for when the API is
preferred; it is not a proxy.

## Leak detection — when eviction does not return the memory

Harmony plans against *measured* free memory, which protects it from footprints
that under-declare. It does not protect it from a node that has already evicted
everything and still holds the VRAM.

That happened on 2026-08-04: a polytts node reported every unit
`resident: false` while its process held **14.7 GB** in PyTorch's caching
allocator. Declared state said "nothing resident", the driver said "full", and
the planner had no lever — you cannot evict what is already evicted. polyasr on
the same card then failed every request with `CUDA out of memory. Tried to
allocate 2.00 MiB`, which reached users as empty transcripts and a 500 on batch
recovery. Nothing in the system could state the condition.

Two additions, both in `meters.py`:

- **`cuda_self_meter()`** — what THIS process holds, split into live tensors
  (`allocated_bytes`) and the allocator's reserved-but-unused pool
  (`reclaimable_bytes`). `cuda_meter` answers "how full is the card";
  this answers "who is holding it, and is any of it reclaimable".
- **`leak_signal(self_usage, resident_footprint_bytes)`** — names the gap when a
  process holds materially more than its resident units explain. Default slack
  1.5 GB, because kernels, activation and fragmentation cost real memory no
  footprint declares, and a signal that fires on healthy nodes is one people
  learn to ignore.

Both surface in `GET /residence` as `process_mem` and `leak`, so the condition is
visible to the broker and to anyone reading a node — an hour after it starts
rather than after a neighbour dies.

### The lever: `POST /model/reclaim`

Detection alone could not have fixed that outage. Harmony's only lever is
evicting **units**, and the leaked memory belonged to no unit — it was already
evicted. Only the owning process can hand its allocator pool back, so the node
facade exposes `POST /model/reclaim`: it runs `gc.collect()` + the backend's
`freeing.free_cuda()` / `free_mlx()` / `trim_ram()` **on the GPU executor** (so it
cannot race a load or a generate) and reports before/after, so the caller can see
whether anything actually came back rather than assuming.

The broker closes the loop in its reconcile pass: `sweep_leaks()` asks any peer
reporting a `leak` to reclaim, **before** planning — reclaimed memory changes what
fits, and a plan built against a card that is about to gain 14 GB would evict
things it does not need to. Throttled per peer (`LIVESTACK_RECLAIM_INTERVAL`,
default 120 s), because reclaim touches the GPU executor and a node whose pool
does not come back is reporting a genuine leak rather than a stale cache — worth
a log line, not a tight retry loop. One peer failing never stops the sweep.

So the guarantee is now: **Harmony prevents over-subscription, detects
unexplained memory, and asks the one process that can release it to do so.** What
it still cannot do is stop a process from leaking in the first place.

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
- **zz-tower2** (no GPU): `livestack-buildd.service` — the peerless **build broker**
  (:8800) arbitrating lodestar build hosts; see "Build hosts" above.

## Tests

```
cd node-py && python -m pytest -q          # planner, broker, facade, federation, lease
cargo test -p livestack-shared             # the Rust residency core
```
