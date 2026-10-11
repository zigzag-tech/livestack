## MODIFIED Requirements

### Requirement: Memory on a measured host is charged at learned claims

A job that states no memory `admit` SHALL be charged, on a measured host, its handler's
maximum non-reclaimable (else cache-inclusive) memory peak over the last 20 succeeded
attempts that ran under the same declared `need` memory, times 1.25 and bounded by
`need`, once at least 5 such attempts exist; before that it SHALL be charged `need`.
A job that states a memory `admit` SHALL keep the existing claim bounded by `admit` and
`need`. The scheduler SHALL be asked for the claim, not the default `admit`.

#### Scenario: Measured handler with no stated admit
- **WHEN** five succeeded attempts under `need` 8 GiB peaked at 2 GiB and 3 GiB is free
- **THEN** a new job with `need` 8 GiB and no `admit` is claimed (charged 2.5 GiB)

#### Scenario: Peaks under a smaller need are not borrowed
- **WHEN** the samples ran under `need` 8 GiB and the job has `need` 16 GiB
- **THEN** it is charged 16 GiB

### Requirement: Cores are charged at the measured peak for a job that states no cpu admit

A job that states no cpu `admit` SHALL be charged its handler's maximum `cpu_peak_cores` (the
busiest 5 s window of the attempt cgroup) over the last 20 succeeded attempts that ran under the
same cpu `need`, times 1.25, rounded up to a quarter core, at least 1 core (or `need` if smaller)
and at most `need`, once at least 5 such attempts exist; before that it SHALL be charged `need`.
The mean (`cpu_cores`) SHALL NOT lower a claim. A running attempt SHALL be charged the same
claim, not its stored declaration. A stated cpu `admit` SHALL be charged as stated.

#### Scenario: Measured handler with no stated cpu admit
- **WHEN** five succeeded attempts under cpu `need` 6 peaked at 1.0 core and 2 cores are free
- **THEN** a new job with cpu `need` 6 and no `admit` is claimed (charged 1.25)

#### Scenario: A mean is not a peak
- **WHEN** the history carries only `cpu_usage_usec` and `execution_seconds`
- **THEN** the job is charged `need`
