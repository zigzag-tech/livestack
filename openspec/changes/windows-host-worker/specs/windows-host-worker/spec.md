## ADDED Requirements

### Requirement: A Windows host runs each attempt in a named Job Object
A worker on Windows SHALL run each attempt inside its own Job Object, named
deterministically from worker and attempt, with the wrapper assigned to the job
before its first instruction runs, and SHALL NOT acknowledge cleanup or release
capacity while any process of that job survives.

#### Scenario: Attempt is stopped while a detached grandchild runs
- **WHEN** the worker stops an attempt whose handler spawned a child that spawned a detached grandchild
- **THEN** every process of the job is gone before `stop` returns
- **AND** the job object no longer exists

#### Scenario: Worker restarts mid-attempt
- **WHEN** the worker process that started an attempt goes away and a new one starts
- **THEN** the attempt keeps running, the new worker finds its job by name, and stops it before reporting cleanup

### Requirement: Windows attempts are bounded by the kernel
The job SHALL carry the attempt's memory cap as its commit limit, its task cap as
its active-process limit and its CPU need as a hard CPU rate; the wrapper SHALL
kill the rest of the tree on a memory-limit notification and record `oom_kill`,
record a refused spawn as `pids_max_events`, and enforce wall time, so the worker
classifies breaches as infrastructure exactly as on Linux.

#### Scenario: A handler exceeds its memory cap
- **WHEN** a handler commits more than the attempt's memory cap
- **THEN** the tree is killed and the receipt carries `oom_kill` 1 and a peak within a MiB of the cap

#### Scenario: The enrollment probe reports the kernel's limits
- **WHEN** `harmony.probe.v1` runs on a Windows worker
- **THEN** its report carries the job's memory limit and effective CPU quota, read from the kernel, and isolation `windows-job-object`

### Requirement: The Windows worker is a restarting service
The worker SHALL run as a Windows service under a non-administrator account; a
stop request SHALL report stopped; a worker that dies SHALL fail the service so
the service manager restarts it.

#### Scenario: The worker thread dies
- **WHEN** the worker cannot start (for example its configuration names no handler)
- **THEN** the service process ends without reporting stopped and the service manager starts it again

### Requirement: A Windows worker's memory report counts co-resident VMs
The Windows worker SHALL report available memory as the host's physical
available memory less its reserve, in which a WSL or Hyper-V VM's memory is
already in use, and SHALL NOT publish the Linux `host` block, so a WSL guest
worker enrolled under the same physical host keeps its own view while every
admission on the machine is charged against both.

#### Scenario: Worker report on Windows
- **WHEN** a Windows worker reports
- **THEN** `available.memory_bytes` is at most the host's physical available memory and the report has no `host` block

### Requirement: Windows compilation is a policy class
`windows` SHALL be a compilation class granted only by operator host policy; a
handler classified `windows` SHALL be refused on any physical host whose policy
lacks it.

#### Scenario: A Linux host advertises a Windows handler
- **WHEN** a worker on a host without `windows` advertises a `windows` handler
- **THEN** placement refuses it with `compilation_not_admitted: operator host policy`

### Requirement: Windows launch verification proves Job Object membership
The verifier on Windows SHALL run as LocalSystem on a named pipe that admits
only SYSTEM and the worker account, SHALL identify the peer process from the
pipe, SHALL admit only a peer the kernel reports inside the attempt's Job Object
(named from the configured worker and the attempt), SHALL check the job's
kernel-held memory and CPU limits against the authority receipt, and SHALL
refuse when the machine's `MachineGuid` differs from its configuration. A
client SHALL refuse a pipe served by any process that is not LocalSystem.

#### Scenario: Copied metadata from outside the job
- **WHEN** a process outside the attempt's job presents the attempt's metadata
- **THEN** the launch is refused with `compilation_peer_outside_attempt`

#### Scenario: Admitted Windows compiler launch
- **WHEN** a process inside the job requests the `windows` class granted to its host
- **THEN** the verifier returns the authority receipt and the MSVC toolchain runs

#### Scenario: A same-account process squats the verifier's pipe
- **WHEN** the registry names a pipe served by a non-LocalSystem process
- **THEN** the client refuses with `compilation_verifier_peer_untrusted`
