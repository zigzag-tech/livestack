## ADDED Requirements

### Requirement: Desired worker state is declared, versioned and schema-validated

The authority SHALL hold a rollout spec (`rollout-spec.v1`, closed keys, at most 64 KiB) naming per capability set the deployment unit, minimum claiming workers, canary, smoke probes and window, and per worker an optional unit pin or `hold`. Writes SHALL carry the expected generation and SHALL be refused with the current generation when it differs. Unknown keys SHALL be refused by name and the previous spec SHALL stay in force.

#### Scenario: Two writers race
- **WHEN** two operators submit a spec against the same generation
- **THEN** the first succeeds and the second is refused with `generation_conflict` and the current generation, and neither write erases the other's content

#### Scenario: Invalid spec
- **WHEN** a spec contains an unknown key or a `min_claiming` of zero
- **THEN** it is refused by name and the previous spec remains active

### Requirement: Claims are authority state with compare-and-swap and an expiry

Whether a worker may claim work SHALL be held in the authority store, not read from a configuration file after first import. Every drain SHALL carry an owner, a reason and an expiry no later than the configured cap, and SHALL be refused as `drain_requires_expiry` otherwise. A change SHALL name the expected generation. An expired drain SHALL re-enable the worker and write a ledger record, except for a worker marked `needs_operator`. Running attempts SHALL keep their access when a drain starts.

#### Scenario: Forgotten drain
- **WHEN** a drain with a one hour expiry passes its expiry and nobody enabled the worker
- **THEN** the worker claims again, a `drain_expired` record names the owner, and the roster no longer shows it drained

#### Scenario: Concurrent drain by another owner
- **WHEN** worker A is drained by owner `rollout` and a different owner drains it without force
- **THEN** the request is refused as `drain_held_by:rollout` and the existing drain is unchanged

#### Scenario: Stale generation
- **WHEN** an enable names a generation older than the stored one
- **THEN** it is refused as `generation_conflict` and nothing changes

#### Scenario: Legacy file value
- **WHEN** a worker has a stored claims row and `authority.json` carries a different `claim_enabled`
- **THEN** the stored row wins and reload logs `claim_enabled_in_file_ignored` for that worker

### Requirement: Reload behavior is observable

The authority SHALL expose the time, content hash and outcome of its last configuration reload and the reason for any refusal. State held in the store (claims, rollout spec) SHALL NOT depend on a reload.

#### Scenario: Edited file not yet applied
- **WHEN** an operator edits the configuration file and has not sent SIGHUP
- **THEN** `reload/status` shows the last applied hash differing from the file hash

### Requirement: A change activates on one canary worker and passes smoke before fan-out

For a set whose declared unit differs from what its workers run, the reconciler SHALL stage the unit on one canary worker, drain it, wait until it has no running attempt, activate, and run smoke jobs that are real launches through normal placement before re-enabling it. Smoke probes SHALL come from a closed vocabulary including worker restart health, handler import, handler integrity, rootless docker start, compilation launch and capture size. A probe that does not apply SHALL be reported `not_applicable`, never passed. On failure or timeout the canary SHALL be rolled back to its previous unit, re-enabled, the unit SHALL be marked rejected for the set with the failing probe named, and no other worker SHALL be changed.

#### Scenario: Rootless docker broken by a unit setting
- **WHEN** a worker release sets an isolation option that makes the container runtime fail to map user ids
- **THEN** the canary's `rootless_docker_start` probe fails as `smoke_failed:rootless_docker_start`, the canary is restored, and no other worker restarts

#### Scenario: Handler fails import
- **WHEN** a bundle references an undefined name or redeclares a binding
- **THEN** `handler_import` fails on the canary within the probe bound, before any customer attempt uses the bundle

#### Scenario: Stale verifier copy
- **WHEN** the unit's verifier digest differs from the canary's root-owned copy and the helper is not installed
- **THEN** the set reports `verifier_manual`, nothing is activated, and the roster says the operator step is pending

#### Scenario: Canary passes
- **WHEN** all required probes pass and the soak period ends without unit-attributed failures
- **THEN** the remaining workers are rolled one at a time, each with the reduced smoke set

### Requirement: Rollout is gradual, bounded and never below the declared minimum

The reconciler SHALL change at most `max_unavailable` workers of a set at a time and SHALL NOT begin a step that would leave fewer than `min_claiming` workers of the set able to claim. It SHALL NOT kill a running attempt. Two consecutive failed steps in a set SHALL pause the rollout and re-enable every drained worker. A worker that is unreachable, stale, or held SHALL be skipped and reported, with attempts per unit capped. Mode `off` SHALL perform no action and `observe` SHALL only record what it would do.

#### Scenario: Minimum would be violated
- **WHEN** a set of three workers has `min_claiming` 3
- **THEN** no step starts and the roster says `waiting:min_claiming`

#### Scenario: Pause after repeated failure
- **WHEN** two consecutive workers fail activation
- **THEN** the set shows `paused:failures`, no worker remains drained by the rollout, and an operator must resume

#### Scenario: Observe mode
- **WHEN** mode is `observe` and a worker is behind its unit
- **THEN** a ledger record states the action that would be taken and the worker is not drained or restarted

### Requirement: Failed activation rolls back; rollback failure fails closed

After activation, a restart crash loop, a failed smoke probe or an exceeded failure budget attributable to the unit SHALL trigger rollback to the previous retained unit. If rollback itself fails the worker SHALL remain drained, SHALL be marked `needs_operator`, and SHALL NOT be re-enabled by drain expiry.

#### Scenario: Rollback also fails
- **WHEN** restoring the previous release fails its restart health probe
- **THEN** the worker stays drained as `needs_operator` after any drain expiry and appears in the roster and the needs-you view

### Requirement: Worker release, handler bundle and verifier copy form one deployment unit

A deployment unit SHALL be an immutable, content-addressed manifest binding the worker release digest, handler bundle digests, verifier payload digest, runtime capture size with its cap, a minimum authority version and the source commits. Building a unit SHALL fail when a part is absent, when the capture exceeds its cap, or when handlers fail an offline import check. Workers SHALL report which part digests they run, and the roster SHALL show per worker `current`, `behind`, `unit_mismatch` or `unknown`. A mismatched part SHALL withdraw eligibility only for work that needs that part.

#### Scenario: Handler updated, verifier not
- **WHEN** a worker runs the new handler bundle but the previous verifier copy
- **THEN** the roster shows `unit_mismatch:verifier`, compilation placement excludes it, and non-compilation work continues

#### Scenario: Legacy worker
- **WHEN** a worker reports no unit facts
- **THEN** it is shown `unknown`, keeps serving its explicit handlers, and is not reconciled

#### Scenario: Capture over its cap
- **WHEN** the built capture exceeds the declared cap
- **THEN** the unit build fails naming the size and the cap

### Requirement: Rollout adds no new authority

Reconciliation SHALL use only the existing worker poll channel and existing installers, SHALL carry no key material in a spec, unit manifest, descriptor or smoke job, and SHALL NOT give a worker root. Refreshing a root-owned verifier copy SHALL be possible only through an operator-installed helper that selects among payloads already staged by the operator and verifies the unit digest. Activation on workers serving signer or keystore handlers SHALL wait for an explicit operator approval.

#### Scenario: Release handler worker
- **WHEN** a unit changes the bundle on a worker serving `release.hub`
- **THEN** the reconciler stages it and reports `awaiting_approval`, and activates only after an operator approves and no publish stage is running on that worker

#### Scenario: Secret in a unit
- **WHEN** a unit manifest or spec contains a credential-like field
- **THEN** it is refused by name
