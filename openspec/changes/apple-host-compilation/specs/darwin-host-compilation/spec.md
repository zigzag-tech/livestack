## ADDED Requirements

### Requirement: A macOS host runs admitted compilation in a supervised launchd job
A worker on macOS SHALL run each attempt as its own launchd job in the worker
user's GUI domain, identified by a deterministic label derived from worker and
attempt, and SHALL NOT acknowledge cleanup or release capacity while any process
of that job's tree or process group survives.

#### Scenario: Attempt is cancelled while a compiler grandchild runs
- **WHEN** the worker stops an attempt whose handler spawned a child that spawned a compiler
- **THEN** every process of the tree is gone before `stop` returns
- **AND** the launchd job no longer exists

#### Scenario: Worker restarts mid-attempt
- **WHEN** a restarted worker reconciles a journaled attempt
- **THEN** it finds the job by its label and stops it before reporting cleanup

### Requirement: macOS attempts are bounded by memory, tasks and wall time
The bounded wrapper SHALL enforce the attempt's memory cap on the physical
footprint of its process tree, its task cap on the tree's process count and its
wall-time cap, SHALL kill the tree on breach, and SHALL record the breach in the
exit receipt as `oom_kill` or `pids_max_events` so the worker classifies it as
infrastructure.

#### Scenario: A handler exceeds its memory cap
- **WHEN** the tree's physical footprint exceeds the attempt's memory cap
- **THEN** the tree is killed and the receipt carries `oom_kill` 1

### Requirement: macOS launch verification proves ancestry from the launchd job
The root verifier on macOS SHALL authenticate the peer with `LOCAL_PEERCRED` and
`LOCAL_PEERPID`, SHALL take the attempt's root PID from launchd, SHALL admit only
a peer whose parent chain reaches that PID with the same start time, SHALL check
the limits launchd holds for the job against the authority receipt, and SHALL
refuse when the machine's `IOPlatformUUID` differs from its configuration.

#### Scenario: Copied metadata from outside the job
- **WHEN** a process outside the attempt's tree presents the attempt's metadata
- **THEN** the launch is refused with `compilation_peer_outside_attempt`

#### Scenario: Admitted Apple compiler launch
- **WHEN** a process inside the job requests the `apple` class granted to its host
- **THEN** the verifier returns the authority receipt and the compiler runs

### Requirement: Apple compilation is a policy class
`apple` SHALL be a compilation class granted only by operator host policy; a
handler classified `apple` SHALL be refused on any physical host whose policy
lacks it, whatever tools that host reports.

#### Scenario: A Linux host advertises an Apple handler
- **WHEN** a worker on a host without `apple` advertises an `apple` handler
- **THEN** placement refuses it with `compilation_not_admitted: operator host policy`
