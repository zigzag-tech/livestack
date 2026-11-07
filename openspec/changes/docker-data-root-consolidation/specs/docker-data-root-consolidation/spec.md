## ADDED Requirements

### Requirement: Docker data-roots are bounded per host

The sum of all `docker_cache` namespaces on one host SHALL be bounded by a configured byte cap. When an
attempt begins and the sum exceeds the cap, the worker SHALL remove least recently used namespaces that are
not locked and are not the requesting namespace, until the sum is under the cap, and SHALL record the
eviction in the attempt outcome. A locked namespace SHALL never be removed.

#### Scenario: Over the host cap
- **WHEN** three unlocked namespaces together exceed `host_max_bytes` and a fourth attempt begins
- **THEN** the oldest unlocked namespace is removed and the outcome names `host-cap-evicted`

#### Scenario: Locked namespace is safe
- **WHEN** the oldest namespace holds its slot lock
- **THEN** it is skipped and the next oldest unlocked namespace is removed
