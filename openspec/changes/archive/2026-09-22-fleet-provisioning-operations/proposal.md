## Why

`fleet_scheduler.schedule()` can decide to `Provision` or `Deprovision` elastic
capacity, and nothing dispatches those actions: `POST /fleet/admit` consumes only
`Admit`/`Queue`, and `fleet_dispatch.py` is a test-only lease→run→release helper
whose `Deprovision` is a no-op. The fleet can therefore observe that it is out of
capacity and can never buy any. Media-corpus's digest routing (`fleet-broker.md`
§5.4) is shipped but left OFF for the same family of reasons.

When the deterministic path meets an error it has no registered handling for, the
loop today has nowhere to go but a log line. Jingway ships weaves (bounded
code⇄agent repair) and typed decisions (Simple Jev on Harmony's resident 27B);
livestack ships the classifier transport. Nothing joins them to the fleet loop.

Design record: `_plans/fleetd-weave-jev.md` (v2, with the Codex second opinion in
its §7). Stale in the companions this realises: `_plans/fleet-scheduler.md` §8 says
the scheduler is unwired (it is wired via `fleet_admit.py`); `_plans/decision-ledger.md`
says DESIGN (it is shipped as `ledger.py` + `emit_*`).

## What Changes

- **Durable provisioning operations** owned by Python: an operation store with a
  state machine (`intent → creating → created | rejected | uncertain → announced |
  failed → released`), stable operation ids, idempotency keys, provider
  request/instance ids, the authorising principal; restart reconciliation before any
  new create.
- **A versioned planning/operation API on `hostd`**: `POST /fleet/plan` (serialized
  `FleetPlan` over stable job ids, eligible pools, reservations, effective policy),
  `POST /fleet/operations` (atomic claim-then-dispatch of one proposed action against
  quota — owner AND prefix — and pending creates; region enforced), `GET
  /fleet/operations/{id}`.
- **Reusable fleet workers** as the provisioning semantics: a `Provision` creates an
  instance that announces itself and serves until drain-gated deprovision. Ephemeral
  single-workload dispatch is untouched.
- **Ledger extension**: operation records in the existing `ledger.py`; `emit_admit`
  records `lease_id`.
- **`livestack/fleetd/`** — a jingway-bound TS supervision loop (weave) over that API
  with registered deterministic recovery workflows keyed by structured error class.
- **Simple Jev failure classification** as a shadow-mode decision leaf for
  unfamiliar incidents: five failure classes, each mapped by code to one registered
  workflow. Qualification on frozen cases before `serve`; `serve` is a separate
  explicit activation, not part of this change's completion.

## Capabilities

### New Capabilities

- `fleet-provisioning-operations`: durable, idempotent, quota-atomic, restart-safe
  provisioning of reusable fleet workers from `schedule()`'s actions.
- `fleet-supervision-loop`: the bounded weave that drives operations to a correlated
  green gate, with deterministic recovery workflows and a repair ladder.
- `fleet-incident-classification`: Simple Jev failure classification of unfamiliar
  incidents, shadow-first, code-mapped to workflows, qualified before serving.

### Modified Capabilities

None. Host-broker residency authority, `schedule()`'s policy and its LAST_RESORT
guard, region as a pre-scheduling hard filter, and `fleet-broker.md`'s two-brains
rule are unchanged. Any new hard budget rule is proposed explicitly in `design.md`
and, if adopted, enforced on every dispatch path.

## Impact

- `node-py/livestack_node/`: new `fleet_operations.py` (store + state machine),
  `hostd.py` endpoints, `fleet_dispatch.py` gains a reusable-worker adapter path,
  `ledger.py` operation records, tests with fake providers.
- `livestack/fleetd/` (new TS package, jingway dependency).
- `_plans/fleet-scheduler.md` §8 and `_plans/decision-ledger.md` status lines corrected.
- No jingway framework change expected; the Jev rung is composed at application level.

## Non-goals

No model-controlled tier selection, region change, quota override or budget
override. No `serve`-mode activation. No Phase 5 cross-host warm. No throughput /
resting-pressure model (separate change; named in `_plans/fleetd-weave-jev.md` §5.4).
No relay `/inventory` latency (benchday side, independent).
