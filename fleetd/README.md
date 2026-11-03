# `@livestack/fleetd` — the fleet supervision loop

One tick: read complete, fresh host facts from the authenticated
`harmony.fleet/1` account stream, ask the broker for a plan, and carry each action
that costs money through a jingway wovine whose gates read **correlated facts**.

The loop supervises; it does not decide. Fleet observation comes from the
authenticated Hub stream; every placement comes from `POST /fleet/plan`, and
every effect goes through `POST /fleet/operations`. It holds no capacity state
of its own, so restarting it costs nothing — the claim is already on the
broker's disk before the money is spent.

## The green path is free

Gates are deterministic. A tick whose operations reach `announced` inside their
deadline escalates zero times and spends zero tokens. A red gate consults a
registered workflow table keyed by the structured error `{stage, class, code}`
**before** any model sees it; only an incident nobody registered earns a repair
turn. And waiting is a state, not a failure: provisioning takes minutes, so an
operation inside its deadline schedules a wakeup rather than opening a
conversation about a machine that is booting exactly as expected.

## What a repair turn may do

`read_fleet_view`, `read_operation`, `read_ledger_since`, `reconcile_operation`,
`refresh_availability` (capped, scoped, expiring), `schedule_wakeup`,
`request_policy_change`. That is the whole surface, and a test asserts it is
closed.

There is no `provision`, no `choose_tier`, no `relax_region_policy`. Not caution
about models specifically: a surface that could select `LAST_RESORT` would
bypass the lexicographic guard in `fleet_scheduler.schedule()` that makes "last
resort" literal, and that guard is what stops a bad minute from becoming an
expensive hour. `request_policy_change` records a request for a person and
grants nothing.

## Setup

livestack has no jingway submodule, and this change is not the place to add one.
The fleet's convention is one working copy at `~/jingway`:

```bash
npm run link-jingway     # or: JINGWAY_PATH=/path/to/jingway npm run link-jingway
npm test                 # tsx --test src/*.test.ts
npm run typecheck
```

`link-jingway` REFUSES if jingway is not there rather than leaving an
unresolvable import to fail at the first production run. It also links jingway's
own `zod`: a `Handback`'s schemas are jingway `ZodType`s, and two zod instances
in one process produce schemas that are structurally identical and fail
`instanceof` — which surfaces as validation that silently never matches.

### Authenticated fleet observation

The service caller starts one stream source before invoking `fleetTick()` and
passes it as `fleetSource` on every tick:

```ts
const fleetSource = makeConfiguredFleetStreamSource({ log });
await fleetSource.start();
try {
  await fleetTick({ client, fleetSource, jobs, log });
} finally {
  await fleetSource.stop();
}
```

`FLEETD_STREAM_ACCOUNT_ID` is required and must name the account reported by
`benchday client whoami`. Enroll the Benchday resident for that account using
its existing credential store; FleetD passes no credentials in CLI arguments.
`FLEETD_BENCHDAY_BIN` selects the CLI path (default `benchday`), and
`FLEETD_BENCHDAY_INSTANCE` selects its dedicated resident instance (default
`livestack-fleetd`). The source subscribes with Hub route, account scope, and
`harmony.fleet/1`, then renews every 10 seconds. Hub binds each source envelope
to `subject.machine`; the catalog key `node` describes body-level node facts.
The consumer caps each committed or staged generation at 256 partials and 1 MiB,
with each partial at 4,096 serialized bytes. During replacement, both generations
can coexist, so the actual retained maximum is 512 partials and 2 MiB total.
Warm cached receipts remain stale until live samples replace every expected
source. Any stale, refused, degraded, incomplete or over-limit observation
blocks the planning route with a named `unknown`, `stale`, `degraded` or `gap`
state. The host-level bodies are preserved as published, including the absence
of worker rows.

## Where the pieces are

| File | What it owns |
|---|---|
| `client.ts` | The broker's three routes, typed. A refusal is a value (`OperationRefused`), and a transport failure (`BrokerUnreachable`) is never flattened into one. |
| `fleetStream.ts` | The authenticated, account-scoped `harmony.fleet/1` consumer and its bounds / named observation states. |
| `gates.ts` | The deterministic floor. Correlated facts only. |
| `workflows.ts` | What code does about a failure it already understands. Nothing here spends. |
| `handbacks.ts` | The closed effect surface a repair turn may drive. |
| `tick.ts` | `fleetTick()` — the wovine. |
| `observability.ts` | Joins jingway's repair records and step summaries to `operation_id`. |
| `incident.ts` | The versioned incident packet. Required evidence refuses rather than trims. |
| `classify.ts` | The Simple Jev rung: one `failure_class` question, code-owned invariants, the persisted record. |
| `escalation.ts` | The composed host that puts the classifier between the table and a full repair turn. |
| `corpus.ts` | Turning ledger incidents into frozen, grouped evaluation cases. |
| `policy/` | The scheduler policy's improver (openspec `scheduler-policy-routine` group 5): `source.ts` reads the broker's policy record stream, `improver.ts` is one `npm run policy-improver -- --once` run, `approve.ts` is a person's `policy-approve` / `policy-revert`. See "The policy improver" below. |

## One departure from the design sketch

`_plans/fleetd-wovine-jev.md` §3 draws one wovine per tick with the operation steps
inside it. `openspec/specs/fleet-supervision-loop/`
requires escalations to open in a conversation **per job**, so that a blocked
operation does not stop an unrelated job from being admitted. Both hold if the
tick is split where the ownership changes: one read-only block
(`assemble_state` → `plan`) over the whole fleet, then one block per acting job,
run concurrently, each with its own host. The gates and the effect surface are
identical either way; only the conversation boundary moved, and it moved to
where the spec put it.


## The classifier rung (shadow only)

An incident nobody registered a workflow for gets one bounded Choice question —
`failure_class` over `capacity_shortage | request_or_workload_fault |
provider_fault | uncertain_effect | needs_investigation` — on the model Harmony
already serves. **Code** maps the accepted class to exactly one registered
workflow. The model never names a tier, never provisions, never touches region,
quota or budget.

That split is not squeamishness. On four short, distinct triage labels this
transport shows ~10% order instability (jingway `docs/decision-models.md`). Ten
percent is usable for "which workflow should run"; it is not usable for "should
we rent the expensive thing" — and a one-token answer that could select
`LAST_RESORT` would bypass the guard that makes "last resort" literal.

Three refusals, each because the alternative is worse than no answer:

- **No runner-up.** A code invariant that rejects the winner returns
  `violations` and no selection. Taking second place is code overruling a model
  with a guess.
- **No acting in shadow.** The selection is recorded with its order, its
  probabilities and the workflow it *would* have run — and nothing happens.
- **No recursive provisioning.** An unreachable classifier is a durable human
  block. It runs on the fleet's own LLM capacity; bursting to restore it is a
  spending loop with an outage for a trigger.

Evaluation: `scripts/incident-corpus.mts` (capture + freeze, grouped by
operation), `scripts/observe-incidents.mts` (a balanced permutation schedule
against a live classifier), then jingway's `scripts/evaluate-decisions.ts` to
score. `receipts/README.md` says why there is no receipt yet and what would
produce one.


## The policy improver

`npm run policy-improver -- --once` tunes the fleet scheduler's target-choice
weights (`livestack.fleet.choose_target`) from the broker's recorded decisions.
It runs on the broker host, infers nothing, and changes no routing: a proposal
is a document under `proposals/` that a person approves with
`npm run policy-approve -- <id> --shadow`, then `--activate` once the candidate
has passed the shadow rung. `npm run policy-revert -- livestack.fleet.choose_target`
goes back one activation.

| Variable | What |
|---|---|
| `LIVESTACK_POLICY_IMPROVER_DB` | PGLite data dir (default `~/.local/share/livestack/policy-improver/pg`); `artifacts/`, `proposals/` and `scratch/` live beside it. |
| `LIVESTACK_POLICY_BIN` | The `livestack-policy` replay binary (`native/policy`). Required. |
| `LIVESTACK_POLICY_DIR` | The broker's policy dir, as hostd reads it (records under `records/`). |
| `LIVESTACK_POLICY_ADMIN_TOKEN_FILE` | The `policy_admin` bearer token, for `policy-approve`/`policy-revert` only. |

The activation LEDGER, not the activation table, says which artifact is active:
jingway keeps one table row per policy and a proposal overwrites it
(`store.ts` `activeFromLedger`). Tests that replay need `LIVESTACK_POLICY_BIN`
and are skipped, with that reason, without it. The fixture stream under
`src/policy/fixtures/` is regenerated by `scripts/gen-policy-fixture.py`.
