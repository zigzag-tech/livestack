## Why

The handler registry holds ONE default release per handler id for the whole fleet. When that default is built for linux/x86_64 (`benchday.compilation.flutter-check.v1`, registry generation 73), a darwin/arm64 worker refuses it with `handler_runtime_platform_mismatch`, can never commit generation 73, retries every ~2 s, and the roster flags `handler_activation_failed` for a condition that is not a fault: the default simply is not for that machine. Observed 2026-10-11 on `xc-mac-studio-apple` after it rolled to livestack-36da4d4e.

## What Changes

- A worker compares each offered release's manifest `platform`/`architecture` with its own before downloading it. A release for another platform is a named **`platform_not_supported` skip**: not downloaded, dropped from that worker's defaults, generation still committed, reported under a new report key `handler_platform_skips` and shown in the roster as `platform_skips`. It is not an activation failure.
- A worker may declare `required_handler_releases: [handler ids]` in its config. For those, a release for another platform stays a loud activation failure (`required_release_platform_missing: platform_not_supported ...`).
- The authority accepts and validates `handler_platform_skips` (bounded, typed); a worker only sends the key when non-empty, so an older authority keeps accepting reports from workers with nothing to skip.
- Not changed: per-platform defaults (two releases of one handler id selected by platform). Today a platform needing its own release uses its own handler id; the registry key stays `handler_id`.

## Impact

`node-py/livestack_node/workloads/{worker,store,roster,handler_registry}.py`, `docs/worker-claims-and-rollout.md`, spec `workload-handler-releases`. Authority must be deployed before any worker that has a skip to report.
