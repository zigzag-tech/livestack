## Purpose

This capability lets Harmony admit and supervise work on GitHub Actions while keeping the workload authority responsible for identity, attempt fencing, bounded capacity, and verified results.

## ADDED Requirements

### Requirement: A GitHub Actions run is an admitted Harmony attempt

Harmony SHALL route a workload to GitHub Actions only when operator configuration binds its authorized handler to a fixed repository, workflow, runner class, required compilation classes, and bounded provider capacity. Harmony SHALL persist the job and attempt and reserve provider capacity before dispatch. A caller SHALL NOT select an arbitrary repository, workflow, ref, or runner. A provider refusal or outage SHALL NOT trigger execution on another backend without a separately admitted attempt.

#### Scenario: A configured remote handler is admitted
- **WHEN** an authorized caller submits a handler configured for GitHub Actions and a provider slot is available
- **THEN** Harmony persists the attempt and its resource and capability reservation before dispatching the configured workflow
- **AND** the attempt names the registered provider and immutable input digest

#### Scenario: An unconfigured handler cannot choose a workflow
- **WHEN** a caller requests GitHub Actions for a handler or workflow that operator configuration does not authorize
- **THEN** Harmony refuses the request before dispatch and records the refusal reason

#### Scenario: The remote provider has no free slot
- **WHEN** all configured GitHub Actions slots are reserved
- **THEN** the job remains queued or is refused according to its admission policy with a provider-capacity reason
- **AND** no local worker starts it as a fallback

### Requirement: A remote runner proves its GitHub and Harmony attempt identity

Before a remote runner receives compilation authorization, Harmony SHALL verify a GitHub-issued OIDC identity against the configured repository identity, workflow path and revision policy, source commit, dispatch actor, run id, and run attempt. Harmony SHALL cross-check the reported job id and status against GitHub's API, then issue only a short-lived grant scoped to the current Harmony job, attempt, fence, and configured compilation classes. The authority's operator compilation policy SHALL admit those classes on the provider's virtual host. A missing, invalid, replayed, stale, or policy-refused proof SHALL be refused.

The approved workflow SHALL reach Harmony through the existing HTTPS edge relay. The relay SHALL require its existing edge key, forward only GitHub bootstrap, remote worker-control, and content-addressed object routes, cap control bodies at 64 KiB, and strip the relay key before forwarding. The workload authority SHALL remain bound to its private Headscale address and SHALL authenticate the OIDC bootstrap and each attempt-scoped worker request. A runner SHALL NOT join the mesh or receive an unrestricted tailnet route.

#### Scenario: The configured workflow proves the current attempt
- **WHEN** the dispatched workflow exchanges a valid GitHub OIDC token for the pending job and current attempt
- **THEN** Harmony returns an attempt-scoped compilation grant for only the configured classes
- **AND** the authorization decision records the verified identity and grant expiry without recording the raw OIDC token

#### Scenario: A manual or fork workflow has no pending grant
- **WHEN** a manually dispatched run, fork, different workflow, wrong source commit, or wrong dispatch actor requests authorization
- **THEN** Harmony refuses compilation authorization and records the identity mismatch

#### Scenario: A superseded attempt presents an old grant
- **WHEN** an older run presents a grant after the authority has fenced its attempt
- **THEN** the compilation authorization check refuses it and no new compile step is admitted

#### Scenario: A request targets an unapproved authority route
- **WHEN** a runner presents the relay key to request a route outside the fixed bootstrap, worker-control, or object allowlist
- **THEN** the relay refuses the request without contacting the workload authority

#### Scenario: An oversized remote control request crosses the relay
- **WHEN** a runner sends more than 64 KiB to a GitHub bootstrap or worker-control route
- **THEN** the relay refuses it before forwarding any request bytes

### Requirement: Remote execution remains durable, deadline-bound, and fenced

Harmony SHALL persist the remote dispatch correlation, GitHub run identity, status, and attempt fence with the existing workload state. Following an authority or provider restart, it SHALL reconcile an accepted run rather than dispatch an untracked duplicate. Cancellation or deadline expiry SHALL fence the attempt before requesting GitHub cancellation; capacity SHALL remain reserved until GitHub reports the run terminal and the remote runner has completed its cleanup. Late status, heartbeat, or completion from a fenced run SHALL NOT revive the job.

#### Scenario: Dispatch acknowledgement is lost
- **WHEN** GitHub may have accepted a dispatch but the provider did not receive the response
- **THEN** Harmony reconciles by the persisted unique correlation before retrying
- **AND** it does not start a second authorized run while the outcome is unknown

#### Scenario: Harmony restarts during a remote build
- **WHEN** the authority restarts while the GitHub run is active
- **THEN** the authority reconnects the run to the same persisted attempt and fence
- **AND** it does not release or duplicate the attempt reservation

#### Scenario: A remote run is canceled or expires
- **WHEN** the workload is canceled or passes its immutable deadline
- **THEN** Harmony fences the attempt, requests GitHub cancellation, and retains its provider reservation until remote cleanup is terminal
- **AND** any later heartbeat or result is rejected as stale

### Requirement: Remote results are bounded and bound to executed inputs

Harmony SHALL accept logs and output objects from a GitHub run only through attempt-scoped credentials and existing bounded object-transfer rules. Before completion, it SHALL verify object ownership, declared size, content digest, source digest, build identity, GitHub run identity, and current attempt fence. Duplicate or mismatched outputs SHALL fail the attempt and SHALL NOT be exposed as successful results.

#### Scenario: A remote run returns its declared source-bound artifact
- **WHEN** the current remote attempt uploads a bounded artifact and provenance matching its input digest and run identity
- **THEN** Harmony verifies and stores the artifact in its content-addressed store and completes the attempt with its digest

#### Scenario: A remote run returns a stale or mismatched artifact
- **WHEN** an upload names another source, run, attempt, digest, size, or owner
- **THEN** Harmony refuses that object and the attempt does not complete successfully

### Requirement: Provider credentials stay outside workload data

The GitHub API credential used to dispatch and inspect runs SHALL be read from a private operator-owned file populated from an already authenticated GitHub CLI session and SHALL NOT be stored in workload payloads, source objects, logs, or CAS outputs. The provider SHALL pass only explicit non-secret inputs to GitHub Actions and SHALL NOT resolve product signing secrets from the workload payload or authority configuration.

#### Scenario: A remote workload uses a protected product secret
- **WHEN** a configured remote handler needs a product signing secret
- **THEN** the workflow reads it from its explicitly granted GitHub Actions secret environment
- **AND** Harmony stores only the secret reference and no secret value

#### Scenario: The provider credential is unavailable
- **WHEN** the authority cannot load its configured GitHub API credential
- **THEN** the remote provider is reported unavailable and no dispatch occurs
