## 1. Select the safe manager from the admitted assignment

- [x] 1.1 Pass a worker-owned host-identity requirement to the Linux `SystemdExecutor` only when the accepted assignment carries a compilation grant; preserve the existing user-manager route for runtime-only work. Verify command construction and that a missing system-manager capability fails closed. Ledger: no new state; retain existing job, attempt, grant, and named infrastructure outcome.
- [ ] 1.2 Add a real systemd control for the compilation path proving the handler runs as the worker UID/GID, sees root-owned verifier metadata as UID 0, and retains `PrivateTmp`, `NoNewPrivileges`, cgroup limits, and attempt containment. Keep the existing runtime-only private-tmp control passing. Ledger: no additional decision records.

## 2. Document and validate the boundary

- [x] 2.1 Update `_plans/durable-workloads.md` to state that compilation attempts preserve the host verifier's UID view while retaining private temporary mounts and attempt limits. Verify `openspec validate compilation-verifier-sees-host-identity --type change` and `openspec validate --specs`. Ledger: document the execution invariant only; no durable fields or records change.

## 3. Roll the worker release and prove an admitted build

- [ ] 3.1 Build and verify an immutable worker release from the landed Livestack commit. Verify its file hash against the built source and preserve the current release for rollback. Ledger: retain the existing attempt identities; no admission schema change.
- [ ] 3.2 Roll the release through eligible Linux compilation workers one at a time using `node-py/docs/worker-release-rollout.md`; verify no active attempt or cleanup before each restart. Submit a normal admitted Benchday Rust check without a host selector and verify its bounded `rust-check.json` receipt passes. Ledger: record the existing job/attempt/worker identifiers and completion evidence; add no new ledger row.
