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
