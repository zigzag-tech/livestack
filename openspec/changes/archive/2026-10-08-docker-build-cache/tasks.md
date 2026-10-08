# Tasks

Ledger obligation: none (no placement or routing decision changes).

## 1. Core
- [x] 1.1 `docker_cache.py`: settings validation, namespace, lock, state, wipe, size, prune, canary; tests `tests/test_workload_docker_cache.py` (unit, real files and flock).
## 2. Wiring
- [x] 2.1 `docker_command.py` / `docker_native.py` / `docker_runtime.py` / `supervision.py` / `worker.py`; tests: real rootless dockerd cases in `test_workload_docker_cache.py` (hit, concurrent slots, lock recovery, wipe, bound, epoch, disabled, namespace isolation, canary stale injection).
## 3. Docs and rollout
- [x] 3.1 `node-py/docs/docker-build-cache.md` (bound, enforcer, purge, rollback).
- [x] 3.2 Rolled out 2026-10-08 (zz-joe e2e-1,3,4,5); e2e-2, xc-win-1-wsl, xc-win-1-wsl-2 pending; measurements in `node-py/docs/docker-build-cache.md`.
