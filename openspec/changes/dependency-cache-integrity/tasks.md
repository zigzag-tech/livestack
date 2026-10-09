Ledger obligation for every task below: none. The dependency cache makes no placement or routing decision (it is a
per-attempt file copy inside a worker), so there is no ledger record to write; its outcomes are reported in the attempt
result `dependency_cache`.

- [x] `dependency_cache.py`: `tree_digest`, digest in `meta.json`, verified on the restored copy, `no-digest` refused.
  Tests: `test_a_same_size_poisoned_entry_is_refused_and_dropped`, `test_an_entry_without_a_digest_is_never_trusted`, `test_a_swapped_file_and_a_changed_link_change_the_tree_digest`
- [x] `dependency_cache.py`: key and digest in `reused()` / `response.json`; `audit_every`; `replace` in the commit file.
  Tests: `test_the_response_names_key_and_digest_and_audit_is_scheduled`, `test_a_restored_tree_is_replaced_only_when_the_handler_names_it`
- [x] `worker.py`: save only for `succeeded`; store path inaccessible to the attempt sandbox.
  Tests: `test_only_a_succeeded_attempt_writes_the_store`; the mount policy was checked by hand with `systemd-run --user -p PrivateTmp=yes -p InaccessiblePaths=...` on zz-joe (access refused, other paths writable)
- [x] `node-py/docs/dependency-cache.md`
- [x] build a worker release and roll it to zz-joe-release / zz-joe-release-2 (benchday `release-build-cache` task 6): `livestack-30a6b9af`, verifier SDK `verifier-sdk-30a6b9af` for the two release verifiers, 2026-10-09; no worker enables `dependency_cache` for a release handler yet
