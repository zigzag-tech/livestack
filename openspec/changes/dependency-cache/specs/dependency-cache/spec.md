## ADDED Requirements

### Requirement: Dependency trees are restored from a bounded per-principal cache
The worker SHALL, when `dependency_cache` is enabled, restore each tree declared in the
job source's `.livestack/dependency-cache.json` whose key has a verified entry in the
submitting principal's namespace, before the handler starts.

#### Scenario: hit
- **WHEN** a second attempt of the same principal declares a tree with identical key files and handler executable
- **THEN** the tree is a byte-identical copy of the stored one and is named `reused` in `HARMONY_DEPENDENCY_CACHE_COMPONENTS`

#### Scenario: principals are isolated
- **WHEN** another principal submits identical key files
- **THEN** it misses

#### Scenario: key change
- **WHEN** a key file's content or the handler executable changes
- **THEN** the attempt misses and nothing stale is restored

### Requirement: Only complete, safe trees are stored
The worker SHALL store a tree only if the handler committed its path, the key recomputed
after the attempt equals the key at restore, and the tree has no absolute or escaping
symlink and no special file.

#### Scenario: no commit
- **WHEN** the handler writes no `dependency-cache-commit.json`
- **THEN** nothing is stored and the result names `handler-wrote-no-commit`

### Requirement: The cache is bounded and never fails an attempt
The worker SHALL keep each namespace under `max_bytes` by evicting least recently used
entries at store time, and SHALL report every restore and store outcome in the attempt
result `dependency_cache`; a cache fault SHALL leave the attempt result unchanged apart
from that report.

#### Scenario: over the bound
- **WHEN** a store would exceed `max_bytes`
- **THEN** the oldest other entries are removed first
