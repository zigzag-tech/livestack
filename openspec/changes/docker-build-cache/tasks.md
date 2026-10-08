# Tasks

Ledger obligation: none (no placement or routing decision changes).

## 1. Core
- [x] 1.1 `docker_cache.py`: settings validation, namespace, lock, state, wipe, size, prune, canary; tests `tests/test_workload_docker_cache.py` (unit, real files and flock).
## 2. Wiring
- [x] 2.1 `docker_command.py` / `docker_native.py` / `docker_runtime.py` / `supervision.py` / `worker.py`; tests: real rootless dockerd cases in `test_workload_docker_cache.py` (hit, concurrent slots, lock recovery, wipe, bound, epoch, disabled, namespace isolation, canary stale injection).
## 3. Docs and rollout
- [x] 3.1 `node-py/docs/docker-build-cache.md` (bound, enforcer, purge, rollback).
- [ ] 3.2 Roll out per `worker-release-rollout.md`; record measurements in the doc.
