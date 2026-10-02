## ADDED Requirements

### Requirement: Workers report the measured host, not only a ceiling

A workload worker SHALL report, with every report, a `host` block measured on the
machine it runs on: total and available memory, memory/IO/CPU pressure (PSI), swap-in
rate, the current memory of every Harmony attempt cgroup on the host, and the current
and learned peak memory of every operator-listed model-server unit. A reading that
cannot be taken SHALL be reported as absent (`null`), never as zero. Worker `capacity`
SHALL be optional; when absent the worker SHALL report the measured host as its
capacity.

#### Scenario: Learned service peak survives a restart
- **WHEN** a listed model server's cgroup reported `memory.peak` 16 GB, then the unit
  restarted and its `memory.peak` now reads 1 GB
- **THEN** the worker still reports `peak_bytes` 16 GB for it

#### Scenario: No PSI on the kernel
- **WHEN** `/proc/pressure/memory` does not exist
- **THEN** the report's memory PSI is `null`, and no placement treats it as zero pressure
  evidence or as pressure

### Requirement: Memory on a measured host is charged at learned claims

On a host whose freshest report carries `host`, placement SHALL compute free memory as
measured available memory, minus the reserve, minus every active attempt's unrealised
claim (claim minus its measured current use), minus the largest unrealised model-server
peak. An attempt's claim SHALL be the maximum recorded `memory_peak_bytes` over its
handler's last 20 succeeded attempts, bounded by the job's `admit` and `need` memory,
and SHALL be the job's `need` memory while no peak is recorded. A queued job SHALL fit
its own claim. Hosts without a `host` block SHALL be placed as before.

#### Scenario: The 2026-10-02 zz-joe shape admits one e2e attempt, not two
- **WHEN** a 31 GiB host reports 27 GiB available with a model server learned at 16 GiB
  peak and 3 GiB current, the e2e handler's learned peak is 10 GiB, and two e2e jobs
  (admit 4 GiB, need 10 GiB) are queued for its two worker identities
- **THEN** one attempt is admitted and the other job stays queued with a reason naming
  the memory claim and free figure

#### Scenario: A learned peak below need admits where need would not
- **WHEN** a handler's last attempts peaked at 2 GiB, its jobs declare need 8 GiB, and
  the host's free memory is 5 GiB
- **THEN** the job is admitted

### Requirement: Memory pressure defers admission

Placement SHALL admit nothing onto a host whose freshest report shows memory PSI
`full avg60 ≥ 5 %` or swap-in at or above 16 MiB/s, and each refused job's reason SHALL
name the pressure figure. Running attempts SHALL NOT be stopped by it.

#### Scenario: Swapping host refuses
- **WHEN** a host reports memory PSI full avg60 7.1
- **THEN** a job that would otherwise fit stays queued with reason
  `host memory pressure: memory full avg60 7.1% (limit 5%)`
