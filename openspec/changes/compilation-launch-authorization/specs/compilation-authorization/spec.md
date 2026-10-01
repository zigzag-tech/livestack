## ADDED Requirements

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
