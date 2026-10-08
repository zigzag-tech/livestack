## ADDED Requirements

### Requirement: Persistent Docker data root is opt-in and per principal
A worker SHALL use a persistent Docker data root only when its `docker_cache` block is valid and enabled, only for Linux rootless Docker handlers, and SHALL derive the root from the assignment's submitting principal so that different principals never share one. Otherwise it SHALL behave exactly as before.

#### Scenario: Second attempt hits
- **WHEN** two attempts of one principal run in sequence with the cache enabled
- **THEN** the second reports `hit` and finds the first's images and BuildKit cache

#### Scenario: Principals are isolated
- **WHEN** attempts of two principals run
- **THEN** they use different roots and neither sees the other's images

#### Scenario: Disabled or invalid
- **WHEN** the block is missing, `enabled:false` or invalid
- **THEN** the data root is the attempt-private `docker-data` and the outcome is `disabled`

### Requirement: A root is never shared by two live daemons
The root SHALL be held under an exclusive lock for the attempt; a busy lock SHALL run the attempt on an ephemeral root, and a crashed holder SHALL release the lock.

#### Scenario: Concurrent attempts
- **WHEN** two attempts of one principal and one path run concurrently
- **THEN** one uses the persistent root and the other reports `cold-locked`

### Requirement: The cache is bounded
Each namespace root SHALL be pruned to `max_bytes` at attempt end while dockerd is up and SHALL be discarded if, after dockerd stops, it exceeds `max_bytes` or `max_growth` times its cold size.

#### Scenario: Over bound
- **WHEN** a root exceeds the bound after pruning
- **THEN** it is deleted and the outcome names `discarded(size)`

### Requirement: Failure falls back to cold, loudly
Unclean previous exit, epoch change, identity mismatch or a daemon that cannot start on the root SHALL wipe it, run cold and name the reason in the attempt result; a cache fault SHALL NOT fail the job.

#### Scenario: Killed attempt
- **WHEN** the previous attempt's cgroup was killed
- **THEN** the next attempt reports `wiped(unclean-previous-exit)`

#### Scenario: Epoch bump
- **WHEN** `epoch` changes
- **THEN** the next attempt reports `wiped(epoch)`

### Requirement: Cold canary
Every `canary_every`-th attempt, and the first on an empty root, SHALL bypass the cache, and fingerprint outputs of equal named inputs SHALL match between warm and cold runs; a mismatch SHALL purge the namespace and be named. A job verdict SHALL NOT be compared.

#### Scenario: Stale entry injected
- **WHEN** a warm run's fingerprint differs from a cold run's for the same inputs
- **THEN** the root is purged and the outcome carries `canary_mismatch`
