## Why

An admitted Rust check on ZZ-Joe reached its authorized worker but failed with `compilation_registry_untrusted`. The host registry is valid; `PrivateTmp=yes` under the user systemd manager places the attempt in a user namespace where root-owned host files appear owned by UID 65534. Preserve the verifier's root-ownership check and the attempt's isolation while making compilation authorization usable.

This implements the worker execution boundary described in `_plans/durable-workloads.md`; that record currently names `PrivateTmp` but omits the host-identity requirement for attempts that call the root verifier.

## What Changes

- Run Linux attempts carrying an admitted compilation grant through the system systemd manager, as the existing worker UID and GID.
- Retain `PrivateTmp=yes`, the backend's existing `NoNewPrivileges` policy, the existing cgroup limits, and the same root-owned verifier registry and socket authentication.
- Refuse the attempt by name if the system-manager launch cannot be established; do not retry under the user manager.
- Add a real systemd control for the UID view and verify a normal admitted Rust check end to end.

## Capabilities

### New Capabilities

- `compilation-verifier-execution-context`: Linux admitted compilation attempts retain host UID identity for root-verifier checks without losing temporary-file and process isolation.

### Modified Capabilities

None.

## Impact

- `node-py/livestack_node/workloads/worker.py` and `supervision.py`.
- `node-py/tests/test_workload_supervision.py` and the worker's integration controls.
- `_plans/durable-workloads.md` and the immutable Linux worker release/rollout on compilation-capable hosts.
