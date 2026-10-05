## Context

`HandlerReleaseRegistry` (handler_registry.py) keeps a durable, bounded catalogue of independently released handler packages. Its bounds were: 64 handler ids, 4 releases per handler, 16 GiB of archives, 4 MiB of manifest metadata, 16 unreferenced staged candidates, plus worker-side limits (256 installed packages, 260 entries). `collect()` deletes releases older than the retention window (at least 24 h) that nothing references, from one set-based evidence query.

On 2026-10-05 a burst of handler generations for one handler (`benchday.e2e.full.v1`) reached the per-handler cap and blocked the next release for about 2.3 hours.

## Goals / Non-Goals

**Goals:** bound storage, not the number of things executed in a period; allow bursts with headroom; never delete anything referenced or of unknown reference status; keep database work independent of the number of releases; make every eviction and refusal visible.

**Non-Goals:** worker-side limits (unchanged; entries are a filesystem constraint, not a storage one); an explicit operator `retire` command (a separate, smaller change); changing the 24 h retention default for ordinary sweeps.

## Decisions

### No per-handler count, one total count

The per-handler count protects nothing the global bounds do not already protect (bytes, metadata, candidates, worker entries). It is removed rather than raised: a high soft limit would still be an arbitrary number that someone has to rediscover. A single total of 256 releases stays as a hard bound because it matches a worker's 256-package cap and the 256-row status listing; it is a bound on entries, which bytes do not cover.

### Eviction relieves a bound before the registry refuses

A stage that would exceed bytes, metadata, the total count, or the unreferenced-candidate bound first calls `_make_room`, which selects the oldest releases that (a) are unreferenced by the shared evidence query and (b) were created at or before `now - burst_min_age_seconds`. It plans greedily oldest-first within a constant cap of 32 rows and deletes **only if the whole deficit is covered**, so a refusal never costs a release. The handler-id bound is not relievable this way and still refuses.

The evidence query is the same SQL the retention sweep uses (one shared helper), so the two can never disagree about what is referenced: current and retained-generation defaults, the newest rollback target per handler, defaults reported by fresh workers, queued/running jobs and running/cleanup attempts. If the query fails for any reason, nothing is evicted, an `handler_release_eviction_evidence_unavailable` event is recorded, and the stage refuses by its capacity name.

### The minimum age protects a race; evidence is the safety net

Between staging a release and the first reference to it (a job submitted by digest, a worker inventory report) the release is unreferenced. The minimum age keeps freshly staged releases out of reach in that window. It is configurable but never below one hour, so burst eviction can reclaim releases much sooner than the 24 h steady-state sweep without touching anything young. The age is not the safety property: complete reference evidence is. Shortening the age reduces margin only for a reference that takes longer than the age to be recorded, which is why the floor is one hour and why a failed evidence query evicts nothing.

### Configuration

`burst_min_age_seconds` lives in `handler_release_policy` in the authority config file, validated by a pydantic model (`extra='forbid'`, strict ints, floor 3600) and again by the registry constructor (not above `retention_seconds`). There is no environment variable. Unset means eviction is off and a full registry refuses by name, which is the behaviour before this change minus the per-handler count.

## Risks / Trade-offs

- [A release is evicted that a not-yet-recorded reference will want] -> the age floor plus complete evidence; the staged release can be restaged (idempotent by digest).
- [Eviction work grows with the catalogue] -> one evidence query and at most 32 deletes per stage, independent of release count (tested by counting statements).
- [Two stages race for the same space] -> both run inside `BEGIN IMMEDIATE`; the byte bound is checked after each stage's eviction (tested with real concurrent HTTP stages).
- [Operators lose a convenient rollback] -> defaults, retained generations and the newest rollback target per handler are never evictable.

## Migration Plan

Deploy the registry code, add `"burst_min_age_seconds": 3600` and a new policy `revision` to the live authority config, restart the authority. Rollback: restore the previous release directory and config, restart. No data migration: the events table already stores free-text outcomes.
