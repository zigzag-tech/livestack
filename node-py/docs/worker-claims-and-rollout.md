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

## Priority classes and holds (`limits.handler_priority`)

Authority config, read at start (a SIGHUP does not reload `limits`):

    "limits": {"handler_priority": {"benchday.release.daemon.linux.v1": 1000, "benchday.e2e.full.v1": 0}}

- A job's effective priority is `max(job.priority, handler_priority[handler])`. Non-starved jobs are placed in
  that order; jobs older than `limits.starvation_seconds` still come first.
- A job that waits only for free CPU/memory/disk reserves them for that round against **strictly lower**
  effective priority. A lower job that still fits beside the reservation starts; equal priority is unbound; a
  running attempt is never preempted.
- A lower job refused only by such a reservation shows blocker `held_for_higher_priority` (job id in the
  detail) in `jobs.placement` and the roster queue reason. Spec: `openspec/changes/handler-priority-classes`.

## Releases for another platform (`platform_not_supported`)

The registry has one default per handler id for the whole fleet. A worker compares every offered release's
manifest `platform`/`architecture` with its own before downloading it. A mismatch is a **skip**, not a
failure: the release is not fetched, the handler is dropped from that worker's committed defaults (it keeps
its base handler config), the generation still advances, and the worker reports it under
`handler_platform_skips` (`{generation, handler_id, release_digest, reason: "platform_not_supported",
release_platform, release_architecture}`). The roster shows it as `platform_skips`; it raises no
`handler_activation_failed`. Deploy the authority before a worker that has something to skip (the key is
sent only when non-empty; an older authority refuses it).

To make "no release for my platform" fail loudly for a handler, list it in the worker config:
`"required_handler_releases": ["benchday.release.macos.v1"]`. Such a mismatch is an activation failure
`required_release_platform_missing: platform_not_supported: ...`. Per-platform defaults for one handler id
are not implemented; a platform that needs its own build uses its own handler id.

## Enforce for listed groups (2026-10-12)

The reconciler also ENFORCES, but only for the groups named in its own config (`rollout-reconciler.json`), one
worker at a time. The authority is unchanged: spec mode stays `observe` and the validator still refuses `enforce`.

    "enforce": {
      "enabled": true,                         # kill switch; re-read every tick
      "kill_file": "~/.config/livestack-workloads/rollout-enforce.off",   # existing file = off
      "state": "~/.local/state/livestack-workloads/rollout-executor.json",
      "hosts": {"zz-joe": ["ubuntu@100.64.0.24"]},                          # roster host -> ssh arguments
      "groups": [{"name": "zz-joe-e2e-canary", "set": "zz-joe-e2e", "unit": "unit-xxxxxxxx", "workers": ["zz-joe-e2e-3"]}],
      "notify_cmd": "benchday notify --title Rollout --tag rollout",
      "soak_seconds": 300, "failure_budget": 2, "attempt_cap": 2
    }

The desired release is the `release` of the group's deployment unit (`cli unit` / `rollout unit`). The release
directory must exist under `release_source` (default `~/.local/share/livestack-workload-releases`) on the reconciler
host; the executor pushes it to the worker's host and the host verifies its content hash.

Per worker: stage -> drain (owner `rollout`, 1 h expiry) -> wait idle -> switch -> smoke -> verify -> enable -> soak.
It never restarts a unit with a job in flight (roster idle, and the host helper refuses when `state_dir/active.json`
exists), keeps the set's `min_claiming`, and never enables a drain it did not make. The switch is ONE drop-in on the
host, `~/.config/systemd/user/<unit>.service.d/~rollout-release.conf` (`~` sorts after every other name, so it wins
and nothing else is touched); only the `<release>/node-py` entry of PYTHONPATH changes, other entries are kept.

Failure after the switch: rollback (delete that drop-in, daemon-reload, restart), the release is REJECTED for that
worker (`state.rejected`), a failure is counted for the group, and the group is PAUSED after `failure_budget`.
A rollback that fails leaves the worker drained with `needs_operator` (an expiry never re-enables it) and pauses
everything (`paused["*"]`). Every one of these notifies through `notify_cmd`.

Signer/keystore workers (the release workers): set `"requires_approval": true` on the group. The executor then only
asks (one notification) and waits until an operator creates `<approve_dir>/<worker>@<release>` (default
`~/.config/livestack-workloads/rollout-approvals/`); one file per worker and release. This is the D9 rule, in its
simplest form; it does not look at `publish.sh` itself, but a worker with a publish stage on it is busy and is
never restarted.

Kill switch (any one): `systemctl --user disable --now livestack-rollout-reconciler.service` (stops all of it,
observation too); `"enabled": false` in the config; `touch` the kill file; or spec mode `off`. An early step is
unwound (worker re-enabled), a worker that already passed smoke and is soaking is left as it is.

Resume after a pause: fix the cause, then remove the entry from `paused` (and the release from `rejected` if it
should be tried again) in the state file while the reconciler is stopped. Check state: `cli rollout status`
(`reconciler_report.body.enforce`) and the ledger lines `rollout_exec_*`.

Hand rollback of one worker: delete `~rollout-release.conf` in its `.service.d`, `systemctl --user daemon-reload`,
restart when idle. Not done by this slice: handler-only activation, worker-pull staging, job-failure attribution in
the soak, authority restarts (those stay with `tools/deploy-authority-release.sh`).

### Enforce record (2026-10-12)

- Reconciler on `livestack-89165c16` (built from main), `enforce` block live on xc-tower-ubuntu; declared unit
  `unit-cb4617d2` = release `livestack-5370c7b7` (content hash `3c1a652c...`, handlers copied from `unit-f38a7baa`, which was
  built from `be1d8a42` and 45 runtime commits behind main).
- Canary `zz-joe-e2e-3`: `livestack-4ea822cd` -> `livestack-5370c7b7`. Drain, idle, switch (2.4 s), smoke (restart_clean,
  handler_import, handler_integrity, release_import: pass, 50 s), enable, 300 s soak, done.
- Rollback drill on the same worker: `livestack-badbad01` (5370c7b7 plus one function using an undefined name). It started
  and passed the restart check; `release_import` failed it (`undefined_name ... an_undefined_name_for_the_rollback_drill`),
  the executor rolled the worker back to 5370c7b7, rejected the release for that worker, re-enabled it. The drill's failure
  count was cleared by hand afterwards. It waited 94 s for a running job before the switch: no job was interrupted.
- `zz-joe-e2e-4` was drained and waiting for a 40+ minute flutter-check job when this was written; the idle wait is bounded
  (`idle_timeout_seconds`, 1 h) and the drain is renewed while it lasts.
- Not rolled, on purpose: `zz-joe-e2e-5` (legacy, drained), the Mac apple worker, the release workers (queued behind the
  approval file).

## Unit auto-build (landed means live for the e2e workers)

`python -m livestack_node.workloads.unit_autobuild --config C [--dry-run]` (module `unit_autobuild.py`). It does by
timer what was done by hand after every runtime commit, for the e2e groups only, and stops at a proposal:

1. Candidate = newest `origin/main` commit touching `node-py/livestack_node`. Nothing happens when the declared unit's
   release already has that runtime tree.
2. Quiet deferrals (no notification): candidate younger than `min_commit_age_minutes` (30), a unit built less than
   `min_interval_hours` (6) ago, or this candidate failed less than `retry_after_hours` (12) ago.
3. Gate, fail closed: the repo's own tests on the candidate's tree (`gate.tests`, default `tests/test_workload*.py`,
   run `nice`d from a `git archive` export), then `scripts/build-worker-release.py build` into `release_source`, then the
   `release_import` smoke probe on that release (the one the executor re-runs on the worker host). Also
   `authority_lag_limit`: if the running authority is more than that many runtime commits behind the candidate it stops
   with "deploy the authority first" (a worker newer than its authority can send keys the authority refuses).
4. The new unit copies `handlers`, `verifier`, `capture`, `min_authority` from the declared unit (handlers are rolled by
   the handler registry, not here) and carries the new release (`built_from` = the candidate). It is registered
   (`rollout/units`), the spec sets of the target groups point at it (compare-and-swap on the spec generation), and
   LAST the reconciler config's target groups get `"unit": "<new id>"`. The executor reads that file every tick and
   rolls the workers one at a time as for a hand-built unit (drain, idle, switch, smoke, verify, enable, soak, rollback).
   A step already in flight keeps the release it started with.
5. A notification names the unit, the release and the groups. A failure at any step notifies ONCE per candidate and
   reason ("Unit auto-build FAILED at gate for 1a2b3c4d: tests_failed:..."), leaves the old declared unit, and never
   rolls anything back or touches a worker.

Never touched, enforced in code: a target group with `"requires_approval": true` (signer/keystore workers) or holding a
worker in `never_workers` (the Mac, `zz-joe-e2e-5`) is a configuration error that stops the run before any call. Those
keep the operator approval file / manual path. The tool uses the reconciler's `rollout` principal and never prints it.

Config (`~/.config/livestack-workloads/unit-autobuild.json`, no secrets; example in xc-setup
`config/machines/xc-tower-ubuntu/unit-autobuild/unit-autobuild.json`): `enabled`, `kill_file`, `repo`,
`reconciler_config`, `state`, `release_source`, `groups` (reconciler enforce group names), `never_workers`,
`min_commit_age_minutes`, `min_interval_hours`, `retry_after_hours`, `authority_lag_limit`, `gate{python,tests,exclude,timeout_seconds}`,
`probe{python,extra_path}`, `notify_cmd`. State: `unit-autobuild.json` (bounded history and failures).

Kill switch: `systemctl --user disable --now livestack-unit-autobuild.timer`, `touch` the `kill_file`, or `"enabled": false`.
Tests: `tests/test_workload_unit_autobuild.py` (real throwaway git repo, the real build script and probe).

Gate scope on xc-tower-ubuntu (2026-10-12): `tests/test_workload*.py` minus eight files that fail on that host
for environment reasons at origin/main (`docker`, `edge_forward`, `http`, `launch_verifier`, `service`, `supervision`,
`task_environment_docker`, `worker`: they need sudo `systemd-run`, a real dockerd or sockets). They are listed in `gate.exclude`;
fix the cause and shrink that list rather than growing it. The rest runs in about six minutes.

Two release builders, one hash (2026-10-15). `tools/build-authority-release.sh` (authority; keeps `_deps`) and
`scripts/build-worker-release.py` (workers) both write `livestack-<sha8>` under `release_source`, and the auto-build
reuses an existing directory of the right commit. Until 2026-10-15 the authority script recorded a different
`content_hash` in `RELEASE.json` (it left `_deps` out), so a unit built on top of such a directory made every worker
refuse the staged tree (`stage:staged_content_hash_mismatch`, `livestack-fc8b905a`, 2026-10-13: four workers, two tries
each, nothing drained, attempts capped in `rollout-executor.json`). Now both scripts record `host_release.content_hash`,
and the auto-build refuses a reused directory whose recorded hash is not the tree's
(`build:release_dir_exists_with_a_different_content_hash`). Older authority-built releases (`2a2f3ee6`, `123818dd`,
`fc8b905a`) still carry the old hash: never name one in a unit; build the worker release with
`build-worker-release.py` under a different directory, or rebuild. A capped (worker, release) pair clears only by
removing its keys from `attempts`/`capped` in `rollout-executor.json` while the executor is idle, or by declaring a new release.

Gate re-run of failures (2026-10-15). The gate runs niced on a busy host: on four runs that day a different timing-sensitive
test failed (`test_workload_host_release`, `..._docker_cache`, `..._progress`), each passing alone and in a full re-run with
the same limits. `run_gate` now runs the failed test ids once more by themselves; a pass there lets the candidate through
(logged as `gate: N test(s) failed once and passed on a re-run`), a second failure fails the gate with the test names as before.

## Stall watchdog and dead authority links (2026-10-15 incident)

**Incident.** After the authority restart (deploy of livestack-29db1192, about 13:08 UTC)
`xc-win-1-wsl-2` went silent at about 13:37 UTC (its last log line was a handler-registry
activation, 21:37:24 local) and stayed so until a manual restart at 14:47 UTC. The authority saw it OFFLINE
for over 6 minutes yet had handed it `e2e.full` job 2f5f3bdd; one of its sockets sat in
CLOSE-WAIT; the host was idle (no `harmony-work-*` unit was ever started for the job). Its sibling
recovered by itself.

**What the evidence rules out.** The worker has no long-poll or event stream: it polls
(`register`, `worker/claim`) with a 15 s per-call socket timeout, retries every 5 s and logs each
failure. A dead or restarted authority therefore already shows as `worker waiting after ...` lines
and a reconnect, and the CLOSE-WAIT proves the kernel delivered the authority's close. A worker
blocked in `recv` on that socket would have been woken by it. The log had no waiting lines at all,
and no work unit started, so the main thread was not retrying: it was wedged inside the claim and
prepare path (lease grant, handler check, input transfer, dependency or docker cache restore,
unpack), in a call that has no deadline of its own (a blocked local lock or `cp`, a stuck
filesystem). The journal and the restarted process destroyed the stack, so the exact call is
unproven; this is why the fix below logs every thread's stack. A worker that holds an assignment
and reports nothing is the failure.

**Fix (general, in-process).** `workloads/stall_watchdog.py`: the main thread announces each
phase with a deadline (`step`, `execute`, `prepare`, `transfer`, `materialize`, `running`,
`cleanup`, `handoff`; limits are the `STALL_*_SECONDS` constants in `worker.py`, each above the
longest bounded work of its phase, the download budget being 3600 s). When a phase overruns, the
watchdog logs `worker stalled in phase ...` with the stack of every thread and exits with
code 70, so systemd restarts the unit and the journal reconciles the held attempt: the authority
then sees it lost and requeues it, instead of it being held silently. Phase changes are logged
(`worker phase: a -> b`), and `worker_service` logs `worker recovered` when a step succeeds after
a fault. Control connections now enable TCP keepalive (30 s idle, 10 s interval, 3 probes), so a
peer that vanished without a FIN fails the next call within about a minute; every call already
had a socket timeout.

**Why not systemd `WatchdogSec`.** Workers also run on macOS and Windows, and the units carry no
`Type=notify`. The in-process watchdog covers all three; it cannot save a process wedged in
native code holding the GIL, which is rare here and still caught by the authority's OFFLINE
state.

**Roll.** Worker code is read at process start: the fix reaches a worker only at its next
sanctioned roll (restart when idle, per the rollout record above). Until then it is exposed to
the same wedge; an OFFLINE worker holding a job is cured by restarting its unit.
