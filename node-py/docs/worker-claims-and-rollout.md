# Worker claims, deployment units and the rollout reconciler (observe)

Status: phases 0-1 of `openspec/changes/declarative-worker-rollout` (2026-10-08). Enforce mode does not
exist yet: nothing here restarts, drains or stages a worker by itself.

CLI: `python -m livestack_node.workloads.cli --config C <command>` where C holds `{"authority","token"}`
of an admin (operator) or `rollout` principal.

## Claims (drain / enable)

    drain WORKER --until 2026-10-09T06:00Z --reason "roll image handler" [--owner NAME] [--if-generation N]
    drain WORKER --ttl 7200 ...
    enable WORKER [--owner NAME] [--if-generation N]
    claims [--export-authority-json]      # who holds which worker; JSON {worker: claim_enabled} for the file
    reload-status

- A drain **must** carry `--until` or `--ttl` (at most 24 h): `drain_requires_expiry`, `drain_ttl_exceeds_cap`.
  When it runs out the worker is re-enabled by the authority (lazily, and on its 30 s tick, ledger
  `drain_expired`). For an intentional long hold, drain again before it ends (a re-drain by the same
  owner is allowed); a spec-level `hold` is reserved for the reconciler.
- Another owner's live drain is refused `drain_held_by:<owner>`; an admin may pass `--force` (ledgered).
- `--if-generation N` is compare-and-swap: a stale value is `claim_generation_conflict` (409). Each worker
  is its own row, so two operators on different workers never overwrite one another.
- A drain never touches a running attempt (it keeps its lease, heartbeats and completes).
- `needs_operator` (set by the reconciler after a failed rollback) does not expire into service; only an
  admin `enable` clears it.
- Ledger: `<state_dir>/rollout-actions.jsonl` (`drain`, `enable`, `drain_expired`, `claim_imported`,
  `file_claim_edit`, `rollout_spec`), bounded 8 MiB x 3 files.

Legacy `authority.json` `claim_enabled` + SIGHUP keeps working (see `authority-principal-reload.md`).

## Deployment unit (`deployment-unit.v1`)

`cli unit build --release-dir R --handlers-root H --digest D... [--verifier-dir V] --capture-size N
--capture-cap C --min-authority main --built-from SHA` prints `{id, manifest}` or fails naming the part:
`unit_release_missing`, `unit_verifier_missing`, `smoke_failed:capture_size:over_cap:N>C`,
`smoke_failed:handler_integrity:...`, `smoke_failed:handler_import:...`. `id` is `unit-<sha8>` of the
canonical bytes; nothing credential-like is accepted. Register it with `cli rollout unit FILE`.

A worker reports the parts it actually runs under report key `unit` when its `worker.json` sets
`"report_unit": true` (and `"unit_verifier_dir"` for the root-owned verifier copy). **Turn this on only
after the authority is upgraded**: an older authority refuses the unknown report key. The roster then
shows `unit_state` per worker: `current`, `behind`, `unit_mismatch:<part>` (parts from different units,
e.g. new handler bundle with the old verifier), `unknown` (legacy worker), `undeclared`. This release
only reports; it withdraws no placement.

## Rollout spec and the observe-mode reconciler

`cli rollout spec FILE [--if-generation N]` posts a `rollout-spec.v1` (compare-and-swap). `mode` is `off`
or `observe`; `enforce` is refused (`rollout_enforce_not_available`). `cli rollout status` shows the spec,
the registered units and the reconciler's last observation.

The reconciler is a separate process with its own `rollout` principal (role `rollout`: may read the
roster, drain/enable with an expiry, post specs/units/its report; may not force, clear `needs_operator`,
reload, submit jobs):

    python -m livestack_node.workloads.reconciler --config reconciler.json   # {"authority","token","ledger"}

It posts what it observes and what it *would* do (`observed_only`), and ledgers that when it changes. It
has no executor.

## Smoke probes

`python -m livestack_node.workloads.smoke --context ctx.json --probe handler_import ...`: probes
`worker_restart_clean`, `handler_import`, `handler_integrity`, `rootless_docker_start`,
`compilation_launch`, `capture_size`. `not_applicable` is never a pass, and the three minimum probes must
actually pass. `handler_import` checks Python statically (syntax, undefined names, redeclared top-level
def/class) and JavaScript with `node --check` (syntax and redeclared bindings; an undefined JS identifier
needs `eslint` on the host and is listed under `not_checked` otherwise). `rootless_docker_start` only
shows the PrivateTmp class when it runs inside the worker's own sandbox (`wrap`: `["nsenter","-t",PID,"-m"]`).

## Rollout record and traps (2026-10-08, round 1)

- **Authority** (xc-tower-ubuntu, `livestack-workload-authority`) now runs `livestack-c295e76a` =
  `livestack-c76585e7` + this change (branch `agent/declarative-rollout-authority`; main carries the same
  commits as `ff05d50a`, `8923e025`). Drop-in: `zzzzzzzzzzzzzzzz-release-current.conf`. Rollback: restore
  `~/.local/state/livestack-workloads/backups/claims-rollout-20261008T225736Z/zzzzzzzzzzzzzzzz-release-current.conf.previous`,
  `daemon-reload`, restart. The DB change is additive (`worker_claims`, `rollout_state`).
- **Trap: a release older than `ff05d50a` crash-loops on this authority.json**, because it holds a
  principal with `role: "rollout"` (`rollout-reconciler`) that older code rejects ("known role"). Seen
  2026-10-08 19:02 when a release built from main before this change was rolled: 9 restarts until the
  release was put back. Any authority release must be built from a commit that contains `rollout_routes.py`.
- **Reconciler**: user unit `livestack-rollout-reconciler.service` on the same host, config
  `~/.config/livestack-workloads/rollout-reconciler.json` (mode 600), observe only, survives authority
  outages (logs `observe_failed`, retries every 30 s). Specs used: `rollout-spec.v1` with sets `zz-joe-e2e`
  (`id_prefix zz-joe-e2e-`, `min_claiming` 2), `zz-joe-release` (2 workers, 1), `xc-win-1-wsl` (2 workers, 1).
- **Canary worker roll** done with the claims API: `zz-joe-e2e-1` drained (`--ttl 10800`, owner
  `claude:declarative-rollout`), let finish its attempt, moved to `livestack-be1d8a42` (= `livestack-86c38445`
  plus only `unit.py`, `smoke.py` and the `report_unit` hook in `worker.py`; verified with `build-worker-release.py
  verify`) with `"report_unit": true`, then enabled. Roll back: delete `zzzzz-unit-report.conf` from the worker's
  drop-in directory and restore `worker.json.bak-report-unit-*`, restart when idle.
- **The worker cannot read the root-owned verifier copy** (`/opt/livestack-compilation/<worker>` is not
  listable by the worker user), so `unit_verifier_dir` cannot work as a worker-side report today; the
  verifier part is reported `None` and units built for this fleet declare no verifier until the root helper
  (task 4.3) reports it.
- **First live run of the probes found a bug the fixtures could not**: `node --check` rejects
  `--input-type=module`, so every `.mjs` handler failed `handler_import`; and scanning a bundle's
  `node_modules` made one package 500 files. Fixed (`ea7953ef`); on the canary's real bundle (20 packages,
  478 source files) `handler_import` passes in ~13 s (one `node` per JS file), `handler_integrity` in 0.1 s.
- **Skew seen on first observation** (zz-joe-e2e): `benchday.e2e.task.v1` only on `-2`;
  `benchday.compilation.rust-check.v1` has an extra release on `-2`; `benchday_image_handler_release` differs
  (`-1/-3/-4` vs `-2/-5`); `android_*` labels missing on `-1`; `-5` runs worker release `925a02fa`, the others
  `86c38445`; the release workers run `70a11344`.
