## MODIFIED Requirements

### Requirement: Package staging and retention are bounded and reference safe

The authority and every worker SHALL enforce configured limits for handler IDs, bytes per package and in total, manifest metadata, total catalogued releases, files per package, staging entries, installed packages, and activation receipts. The authority SHALL NOT limit the number of releases of any one handler: storage is the bound. The worker package root SHALL contain at most 256 installed packages and 260 total entries, including at most three transient download/extraction/pointer entries; startup SHALL remove incomplete transients from a prior crash and refuse an over-capacity root by name. Reference reconciliation SHALL issue a bounded number of database round trips independently of job count. Accepted nonterminal jobs, attempts, defaults, rollback selections, journals, and pending cleanup SHALL protect required packages. Retirement SHALL stop future default selection without rewriting accepted work. Deletion SHALL require a configured unreferenced-age window and complete reference evidence; unknown evidence SHALL prevent deletion.

When staging a release would exceed a byte, manifest-metadata, total-release, or unreferenced-candidate bound, the authority SHALL first evict the oldest unreferenced releases that are at least the policy's configured burst age old, and SHALL do so only when the complete deficit can be covered, using the same complete reference evidence as deletion. The burst age SHALL be a schema-validated authority configuration value of at least one hour and no greater than the retention window; when it is unset, eviction SHALL be disabled. Eviction SHALL never remove a default, a rollback target, a release referenced by a queued or running job or attempt, a release a fresh worker reports as effective, or any release whose reference status is unknown, and SHALL issue a number of database round trips independent of the number of releases. Every eviction and every refusal SHALL leave a bounded event naming the handler, release digest and reason. When nothing can be evicted, capacity exhaustion SHALL refuse staging visibly by name instead of deleting a referenced release.

#### Scenario: A burst of releases for one handler
- **WHEN** an operator stages eight releases of one handler within the storage bounds
- **THEN** every stage succeeds and no per-handler count outcome is produced

#### Scenario: Byte pressure evicts an aged unreferenced release
- **WHEN** staging a release would exceed the byte bound and the oldest unreferenced release is older than the burst age
- **THEN** that release is evicted, an eviction event records it, and the new release is staged

#### Scenario: Referenced packages fill the byte budget
- **WHEN** staging a release would exceed the package cap and every existing package is referenced or younger than the burst age
- **THEN** staging refuses with a named capacity result and preserves all existing packages

#### Scenario: A new default activates during cleanup
- **WHEN** an operator changes the default from A to B while an A attempt still uploads artifacts or awaits cleanup
- **THEN** A remains installed and available to that attempt until all references and the configured age window permit collection

#### Scenario: Reference evidence is unavailable
- **WHEN** the authority or worker cannot establish the complete release reference set
- **THEN** it names the evidence failure and deletes or evicts no potentially referenced package

#### Scenario: Eviction is not configured
- **WHEN** the policy sets no burst age and a stage would exceed a bound
- **THEN** staging refuses by name and evicts nothing

#### Scenario: Destructive retention is unconfigured
- **WHEN** the package garbage collector has no valid positive retention window
- **THEN** collection fails closed while package admission remains bounded and reports any capacity refusal
