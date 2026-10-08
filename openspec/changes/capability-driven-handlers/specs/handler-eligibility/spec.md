## ADDED Requirements

### Requirement: Workers advertise bounded facts, never grants

A workload worker SHALL report a closed, bounded `facts` block (at most 8 KiB) containing its operating system, CPU architecture, CPU count, total memory, total workspace disk, discovered runtimes with versions, probed tools with versions or named reasons, and its compilation verifier state. Facts SHALL be produced by bounded local probes and SHALL be treated by the authority as claims used for eligibility and diagnostics. Facts SHALL NOT grant an access level, a compilation class, a signer, or a host policy. A worker that reports no facts SHALL be shown as `facts_unavailable` and SHALL keep serving its explicit handlers unchanged.

#### Scenario: A worker reports discovered runtimes and a missing tool
- **WHEN** a worker with python3 and no `cargo` registers
- **THEN** its facts name python3 with its version and `cargo` with a probe reason, and neither claim changes any access level

#### Scenario: A legacy worker sends no facts
- **WHEN** a worker on an older release registers without `facts`
- **THEN** the roster shows `facts_unavailable` for it, its explicit handlers keep serving, and no handler is withdrawn or called ineligible

#### Scenario: Facts exceed their bound or use unknown keys
- **WHEN** a report carries facts over the size bound or with an unknown key
- **THEN** the authority refuses the report by name and retains the previous accepted facts

### Requirement: Handler requirements come from three tiers and only the operator grants

A handler's effective requirements SHALL be the strictest combination of: values derived from its release manifest (platform, architecture, backend, runtime); package-declared hints in a `harmony-handler-package.v2` manifest `requirements` object (memory, disk, CPU, probe-vocabulary tools, optional selftest), which SHALL only narrow eligibility; and the operator profile in the authority configuration. Only the operator profile SHALL set the access level, compilation classes, attested labels, and execution policy. A release naming a tool outside the closed probe vocabulary SHALL be unassignable by name. Version 1 manifests SHALL remain valid with no hints. Memory and disk requirements SHALL compare against the worker's capacity ceiling, not its momentary availability.

#### Scenario: A package tries to grant itself a compilation class
- **WHEN** a v2 manifest contains a compilation class, access level or label attestation
- **THEN** the manifest is refused as having unknown fields and nothing is registered

#### Scenario: A package understates its memory need
- **WHEN** the operator profile requires more memory than the package hint
- **THEN** the effective requirement is the larger value

#### Scenario: A release names an unknown tool
- **WHEN** a v2 manifest requires a tool outside the probe vocabulary
- **THEN** the release is staged but reported unassignable with `tool_unknown:<name>` on every worker

### Requirement: The authority decides assignment with a pure, bounded evaluation

The authority SHALL compute, for each fresh worker and each registry handler, one eligibility state and a closed reason, using a pure function of the handler profile, release requirements, worker facts, worker principal, and a snapshot of operator host policy and attestations. The computation SHALL perform no per-pair database access, SHALL be recomputed only when facts, policy revision, profile, or registry generation change, and SHALL cover at most the configured worker and handler bounds. The worker report response SHALL carry the assigned handlers and digests. The worker SHALL evaluate the same function on its own facts and SHALL refuse, with a named reason visible in the roster, any assignment it would not make itself.

#### Scenario: A worker satisfies a handler
- **WHEN** a worker's facts satisfy every requirement of an `open` handler and its mode permits `open`
- **THEN** the next worker report response assigns the handler's default digest and the roster shows `eligible_not_installed`, then `installing`, then `serving`

#### Scenario: A worker lacks memory
- **WHEN** a handler requires 9 GiB and the worker's capacity is 6 GiB
- **THEN** the roster shows `ineligible` with `memory_below: needs 9.0 GiB, has 6.0 GiB`

#### Scenario: The authority offers what the worker would refuse
- **WHEN** a response assigns a handler the worker's own evaluation rejects
- **THEN** the worker installs nothing, serves nothing, and the roster names `authority_assigned_but_worker_refuses` with the worker's reason

#### Scenario: Evaluation work does not scale with job count
- **WHEN** 128 workers and 64 handlers are evaluated after one facts change
- **THEN** only that worker's row is recomputed and the number of database statements is independent of the number of workers and jobs

### Requirement: Access levels bound what assignment can grant

Every handler SHALL have exactly one access level set by operator profile, defaulting to `open`. An `open` handler MAY be assigned to any eligible worker whose mode permits it. A `host_enrolled` handler, one with any compilation class, SHALL be served only where operator host policy allows every class for the worker's physical host and the worker's verifier state is enrolled. An `attested` handler, one needing a signer or equivalent, SHALL be served only where an operator attestation for the physical host provides every label the profile names. Worker-reported values for attested label keys SHALL be ignored and recorded. Assignment SHALL distribute only verified handler code; it SHALL NOT distribute keystores, signing keys, deploy credentials, tokens, or verifier configuration. A worker SHALL be able to deny any handler locally; no worker-side setting SHALL raise an access level.

#### Scenario: Code is installed where the privilege is absent
- **WHEN** a `host_enrolled` handler matches a worker's facts but the host has no policy for its class
- **THEN** the package MAY be present, the handler is not served, and the roster shows `withheld` with `host_not_enrolled:<class>`

#### Scenario: A worker asserts a signer label
- **WHEN** a worker reports `android_signer_sha256` for a host with no operator attestation
- **THEN** the label is dropped from placement, the report records `reserved_label_ignored`, and the signer-requiring handler stays `withheld` with `signer_not_attested`

#### Scenario: A signing handler is assigned to an attested host
- **WHEN** the operator attests a signer for a physical host and the worker's facts match
- **THEN** only the handler code is installed; no credential is in any sync response or package

### Requirement: A handler serves only after verified install and self-test

A worker SHALL serve an assigned handler only after the release is installed through the existing digest-verified atomic installer, every required tool probe passes, and any declared selftest passes in a worker-local supervised attempt under the handler's own backend and resource limits. A failure SHALL leave the handler `quarantined` with a named reason, SHALL keep any previously serving digest serving, and SHALL be bounded and retained in the report. A handler with no declared selftest SHALL be shown as serving without a selftest.

#### Scenario: A new default fails its selftest
- **WHEN** a worker serving digest A installs default B whose selftest fails
- **THEN** A keeps serving, B is `quarantined` with the selftest reason, and no pointer commit occurs

#### Scenario: Install exceeds worker package bounds
- **WHEN** installing would exceed the worker's package count or byte bound
- **THEN** the state is `ineligible` with `worker_package_capacity` and nothing referenced is deleted

### Requirement: Withdrawal, rollback and flapping are bounded and explicit

When a worker stops satisfying a handler, the authority SHALL stop assigning it; the worker SHALL stop advertising it only after every running attempt for it ends, SHALL record `withdrawn` with the reason, and SHALL NOT kill an attempt. Per-handler rollback SHALL remain activation of a retained digest. The authority SHALL limit assignment changes per worker per hour, SHALL debounce facts changes shorter than one report interval, and SHALL name a held change `assignment_rate_limited`. Each assignment decision SHALL leave a bounded event recording worker, handler, digests, state, reason, policy revision, and registry generation, retained in a bounded ring.

#### Scenario: The memory ceiling is lowered while a job runs
- **WHEN** an operator lowers a worker's capacity below a handler's requirement during an attempt
- **THEN** the attempt completes, the handler leaves the report afterwards, and the roster shows `withdrawn: memory_below`

#### Scenario: Facts oscillate
- **WHEN** a probe alternates between pass and fail every report interval
- **THEN** the authority holds the assignment, names `assignment_rate_limited`, and does not install and remove the package repeatedly

### Requirement: The roster states every worker and handler pair with a reason

The authority SHALL expose a matrix with one row per worker and per registry or explicitly listed handler. Each row SHALL have exactly one state of `serving`, `eligible_not_installed`, `installing`, `selftest_pending`, `quarantined`, `ineligible`, `withheld`, `override_serving`, or `facts_unavailable`, and every non-serving state SHALL carry a reason code from a closed vocabulary with a human sentence containing the compared values. `override_serving` SHALL mean a hand-listed handler the evaluator would not assign, with its reason. The handler capacity route SHALL additionally report eligible-not-installed workers and a histogram of reasons.

#### Scenario: A handler is absent and the reason is unknown
- **WHEN** a worker does not serve a registry handler and the evaluator finds no cause
- **THEN** the row shows `eligible_not_installed` or a named internal reason, never an empty cell

#### Scenario: The matrix is read in observe mode
- **WHEN** workers run in `observe` mode
- **THEN** the matrix shows what each would serve beside what it serves, nothing is installed, and no handler list changes

### Requirement: Explicit handler lists become overrides

Precedence SHALL be worker `handler_policy.deny`, then explicit `handlers` entries (pins, served as before, still subject to verified install), then computed assignment. Worker configuration `handler_assignment` SHALL default to `off`, in which behaviour is unchanged. Modes SHALL be selectable per worker among `off`, `observe`, and one enabled level per access class, each reversible by setting the mode and reloading. Per-handler execution policy SHALL move to the operator profile; a worker block MAY only tighten it. The authority's installed handler ids and compilation classes SHALL derive from registry profiles, and a conflict between a typed list and a profile SHALL refuse configuration load by name.

#### Scenario: Default configuration is unchanged
- **WHEN** a worker has no `handler_assignment`
- **THEN** it behaves exactly as before this capability and the authority ignores its facts for assignment

#### Scenario: Deny beats pin and assignment
- **WHEN** a handler is both listed explicitly and denied
- **THEN** it is not served and the roster shows `denied_by_worker`

#### Scenario: A profile contradicts the typed compilation list
- **WHEN** `compilation_handlers` and a profile's `compilation_classes` disagree
- **THEN** the authority refuses to load or reload that configuration and retains the previous complete one
