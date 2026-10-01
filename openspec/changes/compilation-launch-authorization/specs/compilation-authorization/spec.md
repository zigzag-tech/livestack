## ADDED Requirements

### Requirement: Compilation rollout preserves admitted work
Operators SHALL be able to pause new worker claims without revoking current
attempt renewal, launch verification, artifact handoff or completion. A paused
claim SHALL return a named draining reason. Existing authenticated requests may
finish, and deployment SHALL verify actual worker and authority idleness before
replacing a worker. Re-enabling claims SHALL preserve enrollment and boot.

#### Scenario: An operator drains a busy builder
- **WHEN** the operator reloads a worker principal with claims disabled
- **THEN** new claims return no assignment and the reason `worker_draining`
- **AND** the current attempt can renew and complete without being fenced
- **AND** queued work remains available when claims are re-enabled

#### Scenario: Another worker polls during drain
- **WHEN** another worker triggers fleet-wide placement while a worker is drained
- **THEN** placement excludes the drained worker and records `worker_draining`
- **AND** the drained worker does not count as an eligible retry alternative

#### Scenario: Drain configuration is malformed
- **WHEN** a claim-enabled setting is not boolean or disables a non-worker principal
- **THEN** reload refuses the configuration and retains the previous principal set

### Requirement: Operator physical-host policy constrains compilation

The authority SHALL intersect installed handlers, hard capabilities and resource
admission with current operator policy for every compilation class. Enrollment
aliases SHALL resolve to one physical host policy and budget. Missing, expired
or unsupported policy SHALL NOT grant compilation.

#### Scenario: UI-only host reports installed compilers
- **WHEN** a worker or alias on an excluded physical host reports build tools
- **THEN** compilation placement is refused with an attributable reason
- **AND** independently admitted prebuilt device/runtime work remains eligible

#### Scenario: Eligible build worker receives work
- **WHEN** current operator policy permits all required compilation classes and resources fit
- **THEN** an attempt is admitted with policy, physical host and input identity

### Requirement: Compilation launches verify current attempt containment

An authenticated worker-local verifier SHALL authorize launch only for a peer
process inside the current owned attempt containment with matching physical
host, worker boot, input, class, policy revision and attempt fence. Verification
SHALL have a five-second deadline and 16 KiB message bound. Caller-controlled
environment or copied receipts SHALL NOT grant permission.

#### Scenario: Foreign process copies metadata
- **WHEN** a process outside the attempt requests compilation using its metadata
- **THEN** the launch is refused before any compiler or builder starts

#### Scenario: Policy or lease is revoked
- **WHEN** an active attempt loses authorization
- **THEN** further launches refuse and owned descendants stop before capacity is reused

### Requirement: Launch evidence is bounded and attributable

Receipts SHALL name known host, policy, class, job/attempt/input and refusal
cause without credentials. Each receipt SHALL be at most 16 KiB and retained
only as the current receipt for an attempt phase under existing artifact bounds.

#### Scenario: Verifier cannot be reached
- **WHEN** verification fails or exceeds its deadline
- **THEN** the caller reports verification unavailability and starts no compilation

#### Scenario: A caller verifies a completed artifact's producing admission
- **WHEN** an authenticated owner reads a completed compilation job
- **THEN** each bounded attempt entry carries its original recorded compilation grant alongside worker, physical host, boot and fence
- **AND** later policy changes do not rewrite historical producing identity
- **AND** the historical grant cannot authorize a new launch or renewal
- **AND** runtime-only attempts explicitly carry no compilation grant
