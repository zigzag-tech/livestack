# Rollout recheck — 2026-10-09

Recorded 2026-10-09 05:09 UTC.

## Durable changed-assertion cargo

After the owner-authorized ZZOPS coordinator restarts, reattached `zzops train watch` to cargo `tt_743d3c2c-a167-40ba-8c0b-6679063c7543` in train `tt_895e20c3-9f52-4e41-995c-63dd837fd224`. The cargo is terminal `answered`, verdict `pass`, and `partial: true` for the four derived assertions: cleanup and relocation, request and handoff, source/cache freshness, and task-E2E scope. Producing source was `43681dcedea56cf1d96d4556d48d90bacb2e76ed`. No duplicate submission was made. This is change-specific assertion evidence, not full-suite evidence or environment-reuse acceptance.

## Current live diagnosis and prepared release

The shared Livestack authority on 100.64.0.18 still runs release `46a4bd3eab2d3a4bdea4274ebb81dbfb20e71d65` (content hash `703816761ba0b3b9cb85988ed78c88c3ed7d4e8db0ebfb76611a6946c9bfa2b0`). Worker 2 on zz-joe runs `9ab36aebeb66bf764d8f4c21f8b2edf9faccc2b3` (content hash `294e5e6d169bc51c2ae3f827cb63a33289bafcf8f63b51a0f0e10faf3eea1f68`) and advertises the Flutter, Rust, and task-E2E profiles. The active Flutter, Rust, and task-E2E handler manifests include their required source-link/integrity helpers; Flutter also includes the toolchain contract file.

Read-only authority DB and worker-log reads show why repeated jobs rebuilt: prior assignments arrived with no authority replicas, and the worker removed superseded local generations before evaluating same-host reuse. The current task-environment rows include parked Flutter generation 10, Rust generation 4, and task-E2E generation 32 replicas on zz-joe. The same-host preservation fix is already landed at Livestack commit `d55c4c13fcef241231f012b2578d224e1545ac27`.

Built the 245-file authority candidate from `d55c4c13`; content hash `87bd2b9156d2d2e06444b3119c40a8256954639a4b1fa082c8a9de0084cb79e6`. The disposable authority release check passed static analysis, boot, worker registration, and a job round-trip on the authority host. The immutable candidate is staged at `/home/ubuntu/.local/share/livestack-workload-releases/livestack-d55c4c13`; the release verifier reports `IDENTICAL`. No live pointer, config, or process has changed.

## Drain state

A fresh global ZZOPS fence `taskenv-authority-rollout-20261009-v1` was acquired at 05:05:48 UTC for Benchday and Askafox, then drained with `mode: abort` and a 600-second deadline. At 05:16 UTC the drain returned `drained:false` after 600008 ms with full-suite train `tt_33ef694c-76a2-468f-aa67-52b2fe4bffcb` as its only straggler; ZZOPS automatically released both app holds. The authority independently reports one running `benchday.e2e.full.v1` attempt on `zz-joe-e2e-1` and zero cleanup attempts. The attempt was left intact. Do not restart the authority until a later drain succeeds and the authority has zero running attempts and cleanup; do not proceed over stragglers. The old v3 fence was not reused.

No task-specific E2E, full/coalesced E2E, or app publish was started for this recheck.

## Admitted same-source Rust repeat — 2026-10-09 05:34 UTC

Benchday `rust-remote.sh` submitted two successful `check cli` jobs on `zz-joe-e2e-2` using the same environment and identical source digest. Each worker receipt said `rebuilt`, `authority_replica_unconfirmed`, with both `cargo-home` and `cargo-target` newly created; generations advanced 5→6. Compile phases were 28.09 s and 28.73 s. Authority readback after completion shows the generation-6 replica parked on `zz-joe` and zero cleanup attempts. This is direct evidence of the current no-reuse failure. The `d55c4c13` authority candidate remains staged but inactive because the full-suite train still owns an attempt on `zz-joe-e2e-1`; no authority pointer, config, or process changed.

## ZZOPS restart and authority readback — 2026-10-09 05:53 UTC

The Benchday user-directed resubmission for the two-parent merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` passed admission for exactly four changed task-environment assertions. Its completion cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` is dispatched on `xc-win-1-wsl-2` and remains under watch. The separate full-suite train `tt_33ef694c-76a2-468f-aa67-52b2fe4bffcb` is still active on `zz-joe-e2e-1` with a fresh authority lease; two unrelated release attempts are also active. The authority ledger read showed four running attempts total and zero cleanup jobs. No authority restart or worker activation is safe yet.

The Benchday deploy fence reads open through the normal ZZOPS status API. The canonical host-side `zzops-admin fence-status` command fails with `EACCES` while spawning the bundled `esbuild` binary, so no fresh global fence was acquired. The Livestack candidate was not deployed, and no authority pointer/config/process was changed. Wait for terminal task-gate evidence and an idle authority, then use the repaired ZZOPS fence path for the fenced rollout.

## Changed-assertion completion — 2026-10-09 05:57 UTC

The ZZOPS completion cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` answered PASS (`partial: true`) for all four derived task-environment assertions. `status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43` reports `change_gate.verdict=pass`, `complete=true`, `full=false`, produced by train `tt_24a5c5ea-e132-4aab-9de5-846e64de7348` on source `ddf5f8600d9d13efa99854fda4b30e4b7927f19c`.

At the 05:56 UTC authority read, only the pre-existing full-suite attempt remained running; both unrelated release attempts and the selected completion had ended, and cleanup count was zero. The full-suite worker lease/heartbeat were fresh, so it was not canceled. The global fence-status CLI still fails to spawn the packaged `esbuild` (`EACCES`); no fence was acquired and the authority candidate was not deployed. Post-rollout same-source reuse and invalidation checks remain open.

## Post-restart gate and authority recheck — 2026-10-09 06:07 UTC

After the two owner-authorized ZZOPS coordinator restarts, reattached `zzops train watch` to the existing completion cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` for merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43`. It remains terminal PASS for all four derived task-environment assertions (`partial: true`); the `status --change` verdict is PASS, `complete=true`, `full=false`. The old unrelated full-suite cargo watcher still returns 404, but current train status continues to report full-suite train `tt_33ef694c-76a2-468f-aa67-52b2fe4bffcb` as running on `zz-joe-e2e-1`.

Read-only authority checks show that `livestack-46a4bd3e` is still selected, handler registry generation is 54, and the current config maps Flutter, Rust and task-E2E to their intended development/task profiles while full-E2E and publishing handlers map to forbidden profiles. Worker 2 advertises all three task profiles; worker 5 advertises Flutter and Rust. Twenty-two of 27 workers are ready. The authority database has exactly one running attempt—the unrelated full-suite job `cd80695a0e444be6993e738a4799cf98` / attempt `8d61bff5d18b42f9ad1d8da10f78be75`—and no running cleanup jobs. Its heartbeat was 7.7 seconds old and lease had 117.8 seconds remaining; no progress sample is recorded, which is expected for this Benchday handler.

The ZZOPS deploy fence currently reads open. The `d55c4c13` same-host preservation candidate remains staged and verified, but no fence is held and no authority or worker pointer, config or process changed. The old fence was not reused. Because the sole attempt is live and the rollout procedure requires a zero-attempt/zero-cleanup window, it was not canceled. No task-specific E2E, full/coalesced E2E or publishing was initiated for this recheck; post-fix reuse, invalidation and timing evidence remain open.

At 06:14 UTC, rechecked the installed `/opt/zzops/current/node_modules/.bin/zzops-admin fence-status /etc/zzops/service.json` through the canonical `sudo -u zzops` invocation after the coordinator restarts. It still fails with `EACCES` when spawning that release's bundled `esbuild`; no fence was acquired and no alternate launch path or permission change was used.

## Authority turnover recheck — 2026-10-09 06:18 UTC

ZZOPS now records the long full-suite train `tt_33ef694c-76a2-468f-aa67-52b2fe4bffcb` as terminal infrastructure failure (`worker declared an infrastructure outcome`), not a full-suite test verdict. The matching authority attempt `8d61bff5d18b42f9ad1d8da10f78be75` is ended. A fresh status read shows three other attempts running: the unrelated single-assertion admission `herdr-backend.terminal-mouse-click-routes-through-control` on `xc-win-1-wsl-2`, plus two release jobs on `zz-joe-release` and `zz-joe-release-2`; all had fresh worker heartbeats and leases. ZZOPS reports no stranded cargo and its deploy fence remains open. No cancellation, rollback, new fence, authority/worker change, task-specific E2E, full/coalesced E2E or publishing was initiated. The change-specific task-environment gate remains PASS; safe deployment still requires those unrelated attempts to end and a fresh usable fence-admin path.

Turnover recheck — 2026-10-09 06:22 UTC: the Herdr single-assertion run failed with an infrastructure outcome and is queued for a completion retry (`tt_43ab69fc-9e54-411e-a4c4-a65149987881`); it is one unrelated waiting cargo, not a full-suite request. The authority now has three fresh running attempts: one `benchday.compilation.rust-check.v1` on `zz-joe-e2e-1` and two Benchday release jobs on the Joe release workers. The fence remains open. The task-environment completion cargo remains PASS; none of these other requests was canceled or modified.

Retry dispatch — 2026-10-09 06:24 UTC: the Herdr one-assertion completion retry is now active on `xc-win-1-wsl-2`; the authority also has a Rust compilation on `zz-joe-e2e-1` and an Android release on `zz-joe-release`. All three leases/heartbeats were fresh, with no waiting or stranded cargo in the status read. The retry is unrelated to this change. No fence, cancellation, task-environment request or publishing action was initiated.

## Post-restart gate retry and fence-admin diagnosis — 2026-10-09 10:25 UTC

After the owner-authorized coordinator restarts, re-submitted the landed merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` through `zzops train submit`. The request derives the same four task-environment assertions. Its admission cargo `tt_0b619d85-baba-4f65-8d08-f43bcc568176` first encountered infrastructure signature `WorkloadError-3b87865b`, then retried as train `tt_d21b399f-8545-41ce-ae69-07600771e457`, job `8d222092b07c4b8490c71918718b115d`, attempt `a88cd67148794227a660e3b84d038c58` on `xc-win-1-wsl-2`. At 10:25 UTC ZZOPS still reported `Harmony running`; `status --change` returned `No complete change-bound evidence`, so this retry has no terminal verdict yet. The separate full-completion train `tt_bf33eee1-043c-42fa-8820-0b18ece7cdfc` remained active on `zz-joe-e2e-1`; neither train was canceled.

Read-only `zzops-admin fence-status` on `100.64.0.18` exits 126 with `env: 'node': Permission denied`. `namei -l` shows `/usr/local/bin/node` symlinks to `/home/ubuntu/.nvm/versions/node/v24.11.0/bin/node`, while `/home/ubuntu` is mode `750` and owned by `ubuntu:ubuntu`; the `zzops` account cannot traverse it. The deploy fence remains open. No ACL, permission, symlink, service, authority config/pointer, or worker state was changed, and no alternate admin launch path was used. The authority rollout still needs both a zero-attempt/zero-cleanup window and a working canonical fence-admin runtime. No full/coalesced E2E or publishing was started for this change.

## Current live recheck — 2026-10-09 11:52 UTC

Correction: the earlier fence-admin command was run on `xc-tower-ubuntu` (`100.64.0.18`), a managed client. `tools/fleet-hosts.json` names `zz-tower2` (`100.64.0.12`) as the ZZOPS service host. On Tower2, the same documented read-only `zzops-admin fence-status` command succeeds when run from `/`; the SSH login's `/home/ubuntu` working directory is mode 750 and not traversable by `zzops`. The current fence readback is open with no holder. Its event tail records hold `taskenv-authority-rollout-20261009-v1` at 05:05:48 UTC and release at 05:15:57 UTC after the abort-mode drain deadline. No fence or service state was changed in this recheck.

The current change-bound gate for merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` is `pass`, `complete=true`, `full=false`; all four changed task-environment assertions passed. Read-only worker and rollout status show `zz-joe-e2e-1` still running the pre-existing full job `9a214e6c93894a0e9b3c1b5338239a81`, and `zz-joe-e2e-2` running Rust compilation job `b35ee8c245cd4eed88e7e64952b5ac66`, both with fresh leases. The task-E2E handler is advertised only on worker 2 at release label `909844c7b71167b5dfa7ca5193d37cf41184cd28`.

The Livestack observe-mode rollout is at report generation 1533, spec generation 2, and waits on `canary_not_representative:benchday.e2e.task.v1` for unit `unit-f38a7baa`. Rust development generation 6 and task-E2E generation 32 remain parked on `zz-joe`, each with `last_outcome=rebuilt`; this proves parked state but not reuse. Benchday's task-environment default config remains missing, so automatic selection stays disabled. No task-specific E2E, full E2E, publishing, worker change, or authority activation was started.

## ZZOPS status recheck — 2026-10-09 11:57 UTC

The current Benchday change-bound verdict is PASS, complete=true, full=false, for all four changed task-environment assertions on merge 48c6d99f32a5ec93a87b6375953bec2fa93fed43. The historical global status.gate is not the change verdict.

The separate full-product completion train tt_bf33eee1-043c-42fa-8820-0b18ece7cdfc is still dispatched on zz-joe-e2e-1 (job 9a214e6c93894a0e9b3c1b5338239a81, attempt 1af544232a19402a9fdcb58954c1ae02), with no terminal result observed. ZZOPS reports six remote trains outstanding, two shared-tree cargos waiting, and an open deploy fence. No Livestack authority or worker rollout, task-environment workload, full/coalesced E2E, or publish was started during this recheck.

## Full-train retry recheck — 2026-10-09 12:10 UTC

The Benchday change-bound verdict remains PASS, complete=true, full=false, for the four task-environment assertions. Its separate full-product train tt_bf33eee1-043c-42fa-8820-0b18ece7cdfc remains dispatched; current status identifies job 9a214e6c93894a0e9b3c1b5338239a81, attempt 1af544232a19402a9fdcb58954c1ae02 on zz-joe-e2e-1, elapsed about 116 minutes, with no progress sample or terminal verdict.

Associated cargo tt_c9235ec4-97b7-4f12-a644-998ea29b1ab0 records an earlier infrastructure failure on xc-win-1-wsl-2 (job 75f90f33514e4d608ab62492276ab412, attempt 62bdefaa09324d11be754161d410fbff); the retry binds the newest published snapshot 83ce9b1cf3b2 containing commit be1a65d8b353. Its watcher returned 404 and cargo-specific status rejected this ID, but a fresh global status still confirms the train and its current zz-joe attempt are live. This is not a terminal gate result; no cancellation, rollback, task-environment request, authority rollout, or worker rollout was made.

ZZOPS still reports six dispatched trains, two dispatch-pending trains, two shared-tree cargos waiting, and an open deploy fence. No safe authority drain window is available.
