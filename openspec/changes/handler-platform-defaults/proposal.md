## Why

The registry holds ONE default release per handler id for the whole fleet, and a default-selection job is bound to
that one digest at acceptance. A worker on another platform can therefore never run the handler: the x86_64 default is
`platform_not_supported` there (handler-platform-skip), and nothing else may be selected. Benchday's E2E pool is
x86_64-only for this reason, while an aarch64 Lima guest on xc-mac-studio (8 CPU, 16 GiB) sits idle. Before the
registry the same guest ran E2E (2026-09-27 to 10-07); the registry roll took it away
(benchday `docs/harmony-worker-enrolment.md`, "xc-mac-studio-harmony cannot take E2E today").

## What Changes

- **Per-platform defaults.** Activating a release whose manifest platform/architecture differs from the handler's
  current default records it as that platform's default (`<handler>@<platform>/<architecture>` in the generation's
  defaults map) instead of replacing the primary. Same platform, or no default yet: replaces the primary, as today.
  Rollback is the same operation.
- **A default-selection job accepts one digest per platform.** `spec.handler_release` stays the primary (unchanged
  for every existing reader); `spec.handler_release_variants` lists the other platforms' releases from the same
  generation. Explicit-digest jobs are unchanged.
- **Placement pins the matching variant.** A worker is compatible when its inventory has the primary or any variant;
  the attempt records the variant it matched, and the assignment and result identity use the attempt's descriptor.
  Retries keep the job's accepted set, never a newer default.
- **Workers receive their platform's default.** The registry sync keeps its shape (`defaults` maps handler ids only,
  so older workers accept it); its `releases` also carry each platform variant. A worker whose platform has a variant
  uses it as its default for that handler instead of skipping the handler.
- Reference evidence (retention, eviction, required archives) counts variant digests on queued/running jobs.

## Impact

`node-py/livestack_node/workloads/{handler_registry,store,placement,worker}.py`, spec `workload-handler-releases`.
Deploy order: authority first (old workers ignore variant descriptors as platform skips), then the worker that needs a
variant. No schema migration: variants live in the existing defaults JSON and job spec.
