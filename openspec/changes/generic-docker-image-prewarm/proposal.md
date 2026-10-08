## Why

Apps with a persistent Docker build cache (`docker_cache`) pay the full image
build on the critical path of the first job after their image inputs change:
Benchday measured 241 s with warm cargo mounts, 650-700 s on a cold root, while
other cache slots sit idle. Building ahead of time is app-independent: only the
declaration of "what are my image inputs" and "what command builds the image the
way a real job does" is the app's.

## What Changes

- An app publishes a schema-validated **image-warm manifest** (`image-warm.json`,
  version 1): app id, Dockerfile path, the build-context input paths whose
  fingerprint defines the image, and a `warm` block naming the app's own handler,
  payload, selector, `need`/`admit`, `estimate_seconds` and `max_copies`.
  Reference schema and constructor: Benchday `scripts/lib/e2e-image-warm.mjs`.
- **Worker side (this repo, sibling module, not `docker_cache.py`):** a warm
  attempt is an ordinary attempt of the app's handler; the worker already writes
  `docker-cache-session.json` (`persistent: true|false`). The contract for
  handlers: do the image build the real job does, and only when the session says
  persistent, otherwise finish `skipped` with a reason. No host list exists.
- **Trigger side (ZZOPS, companion change):** when the published source's image
  fingerprint differs from the last warmed one, and only while no job for the app
  is queued, submit up to `min(max_copies, idle eligible workers)` warm jobs
  (key `<app>-warm:<fingerprint>:<slot>`, priority 0, deadline), as the SAME
  principal as the app's real jobs (docker_cache namespaces roots by principal).

## Non-goals

Preemption (Harmony has none), editing `docker_cache.py`, per-host configuration.

## Bound

Per fingerprint, at most `max_copies` (<= 16) jobs, each with a deadline; storage
stays inside each worker's `docker_cache.max_bytes`.
