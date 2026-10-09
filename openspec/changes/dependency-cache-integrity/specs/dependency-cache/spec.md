## ADDED Requirements

### Requirement: A restored tree is verified by content
The worker SHALL store, with every entry, a digest of the tree's content, and SHALL, before telling a handler a
tree is `reused`, recompute that digest on the copy it made and refuse the entry when it differs or when the entry
has no digest.

#### Scenario: same-size poisoning
- **WHEN** a stored file is replaced by different bytes of the same length with the same modification time
- **THEN** the next restore of that entry is a miss `verify-failed: digest-mismatch`, the copy is removed, the entry is dropped and the attempt builds cold

#### Scenario: entry from before digests
- **WHEN** an entry's `meta.json` has no digest
- **THEN** it is refused as `verify-failed: no-digest` and dropped

### Requirement: Only a successful attempt writes the store
The worker SHALL NOT store or replace any entry unless the attempt's outcome is `succeeded`, whatever the handler committed.

#### Scenario: failed attempt with a commit marker
- **WHEN** an attempt ends `product_failure` with a valid `dependency-cache-commit.json`
- **THEN** nothing is stored and the result names `attempt-not-succeeded` for each tree that would have been

### Requirement: The attempt sandbox cannot reach the store
The worker SHALL start an opted-in handler's attempt with the store path inaccessible to it.

#### Scenario: store path hidden
- **WHEN** an opted-in handler's process lists or writes under the store path
- **THEN** the access is refused by the unit's mount policy

### Requirement: Audit attempts are scheduled by the worker and reported with their evidence
With `audit_every` N greater than zero the worker SHALL mark every attempt whose attempt-id hash is 0 modulo N as an
audit in `response.json`, and SHALL return for every reused tree its full key and digest.

#### Scenario: audit flag and evidence
- **WHEN** `audit_every` is 1 and a handler asks for a restore that hits
- **THEN** `response.json` has `audit: true` and each reused component has a 64-hex `key` and `digest`

### Requirement: A handler may replace an entry it found wrong
The worker SHALL store a restored tree again only when the handler lists its path in `replace` in the commit file and the attempt succeeded.

#### Scenario: replacement
- **WHEN** a handler commits a rebuilt tree with its path in `paths` and `replace`
- **THEN** the entry for its key is overwritten and the next restore serves the rebuilt tree

#### Scenario: no replace
- **WHEN** a restored tree is listed in `paths` only
- **THEN** it is not stored again
