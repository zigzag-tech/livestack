# Host and fleet broker: pinned release deploy (cherry-pick recipe)

Status: operational runbook (2026-10-14).

On xc-tower-ubuntu, `livestack-hostd.service` (:8799, host broker) and `livestack-fleetd.service`
(:8801, fleet broker) run from pinned release directories under
`~/.local/share/livestack-releases/<name>/node-py`, selected by `Environment=PYTHONPATH=...` in
`/etc/systemd/system/livestack-{hostd,fleetd}.service.d/`. The drop-in `zz-sensenova-admission.conf`
sorts last, so its PYTHONPATH wins in both units. `main` is far ahead of the pinned base
(`sensenova-base-16575876`), so a single change is deployed by cherry-picking it onto that base:

    git worktree add --detach /tmp/ls-<name> 16575876      # the currently pinned base
    git -C /tmp/ls-<name> cherry-pick <sha>                # then run the change's tests
    mkdir -p ~/.local/share/livestack-releases/<name> && cp -a /tmp/ls-<name>/node-py ~/.local/share/livestack-releases/<name>/
    # back up zz-sensenova-admission.conf for each unit (outside the .d directory), then change only its
    # PYTHONPATH line (sed on that line), daemon-reload, restart hostd, wait for /fleet, then fleetd.

Roll back by restoring the backed-up drop-ins, `daemon-reload` and restarting. Release dirs are
never edited in place. Remove the worktree afterwards (`git worktree remove`).

## Deployed: peer pruning (commit 52cb362f), 2026-10-14

Release `peer-prune-52cb362f` = base `16575876` + 52cb362f (only `hostbroker.py`, `hostd.py`,
`membership.py` and `tests/test_membership.py` differ from the base). Peers unseen for more than
24 h are forgotten, seeds included (`DEFAULT_PRUNE_AFTER_S`; `LIVESTACK_PEER_PRUNE_SECONDS`
overrides, `0`/`off` disables, a bad value refuses to start; the daemon sets `prune_seeds` whenever
pruning is on). Startup logs `[hostd] peer prune window: 86400s`. A pruned seed returns at the next
broker start; a registered peer returns when it announces. The previous release is
`sensenova-base-16575876`.

## Deployed: inpainting through Harmony (commits b1da9c93, 724ec228), 2026-11-02

An image request that sends `init_image` and `mask_image` is task=inpaint (derived by the contract, with the
specialist requirement `inpaint: true`); the FLUX.2 Fun Union unit serves it with the controller it already loads.

- Fleet broker (planner): release `admit-wait-on-regional-3ea2476f-depth-specialist-2ce0c3c6-inpaint-b1da9c93` =
  the previous release plus b1da9c93's `requirements.py` (a unit attribute listing values offers each) and its
  `_specialist_only` change (`inpaint` joins `ocr`/`depth_guide`; a unit holding several specialist capabilities is
  served by demand naming any of them), applied by hand where the release's comment differed. Its planner and
  requirement tests plus the two new ones passed in the release before the switch. Drop-in
  `livestack-fleetd.service.d/zzzz-depth-specialist.conf`; backup `/etc/systemd/system/zzzz-depth-specialist.conf.fleetd.before-inpaint-20261102`.
- Image gateway and the FLUX.2 depth unit: release `imagegen-20261102-724ec228` (main at 724ec228). Gateway drop-in
  `harmony-image-gateway.service.d/50-depth-units.conf` (backup `/etc/systemd/system/50-depth-units.conf.gateway.before-inpaint-20261102`);
  process-node config `~/.local/share/harmony-process-node/harmony-image-flux2-depth.json` now points there and its
  unit declares `task: [text_to_image, inpaint]` and `inpaint: true` (backup beside it, `.before-inpaint-20261102`).

Roll back by restoring the three backups, `daemon-reload`, and restarting `livestack-fleetd`,
`harmony-image-gateway` and `harmony-image-flux2-depth`. The SDXL and Qwen image units are unchanged.

## Pending deploy: device leases released across reboot (branch agent/lease-boot-expiry), 2026-11-02

`DeviceLeaseStore` stamps leases with hostd's `time.monotonic` clock, which restarts near 0 at reboot. After
xc-tower-ubuntu rebooted on 2026-11-01, two harmony-image leases from the previous boot (expires_at ~2192059
against an uptime of ~200 s) stayed `exclusive`, blocking admission and a deploy guard, and were released by hand.
Leases now record the kernel boot id (`/proc/sys/kernel/random/boot_id`), and opening the store (hostd start)
releases every active lease from another boot with `reason = previous_boot`. Rows from before this change have
no boot id and are released at the first start, so deploy while `GET /status` shows no `device_leases` held.
**Not yet deployed: xc-tower-ubuntu needs a hostd deploy** (only `device_leases.py` changed; cherry-pick onto the
pinned release as above and run `tests/test_device_leases.py`).

## Deployed: process-node holds cannot leak (commit 91b49f4b), 2026-11-03

Not a hostd change: the fix is in the process-node facade, `process_node.py`. Facade releases live in
`~/.local/share/harmony-process-node/releases/<first 16 hex of the file's sha256>/` (the file plus
`mesh_outbound_py/`, copied unchanged from the previous release), selected by `ExecStart` in each unit's
`<unit>.service.d/zz-harmony-process-node.conf`.

- **polyasr** (`mesh://livestack/tower-asr-1`, 8766/8767): release `b1c2dbe83851f6ef` = `cfb288cc9d14c9ea`
  (identical to main's file at f5cd4793) + 91b49f4b. Drop-in backup
  `/etc/systemd/system/zz-harmony-process-node.conf.polyasr.before-hold-leak-20261103`. Restarting the facade
  stops its worker (it lives in the facade's cgroup); it was idle (`active_requests 0`), and starts again on the
  next admission. After the switch `GET :8799/status` shows asr/align/diarize `busy: false`, facade
  `load.in_flight 0`, and the image gateway's queued exclusive lease on `4bac2869` was granted.
- Every other facade was rolled the same day; see the next section. A leaked hold shows as `load.in_flight > 0`
  on `GET <facade>/livestack/capability` over a worker that reports nothing in flight; restarting that facade
  clears it.

Roll back: restore the backup drop-in, `daemon-reload`, restart `polyasr`.

## Deployed: a busy tenant waits within the caller's window (commit 6190c349), 2026-11-03

hostd on xc-tower-ubuntu runs release `busy-wait-6190c349-on-admit-fifo-e9edbd61` = the previous
`admit-fifo-e9edbd61-on-exclusive-queue-426851de` plus 6190c349's `hostbroker.py` and `device_leases.py`
(applied as a patch; its exclusive tests and `test_device_leases.py` pass against the release, apart from the
boot-id and device-hold tests whose features that release predates). Drop-in `zzz-exclusive-device-leases.conf`;
backup `/etc/systemd/system/zzz-exclusive-device-leases.conf.hostd.before-busy-wait-*`. Deploy/rollback script:
`~/.local/share/livestack-releases/deploy-busy-wait.sh [--rollback]` (waits for no active lease and no open
:8211/:8227 request). Only hostd restarted; the image gateway already passed `harmony_wait_s` as `queue_wait_s`.
The boot-id lease change (agent/lease-boot-expiry, above) is still NOT in this release.
Restarting hostd also restarts `polyasr.service` (seen at 02:49:56 and 03:01:48): drop-in
`polyasr.service.d/99-harmony-demand-policy.conf` has `Requires=`/`BindsTo=livestack-hostd.service`, so treat a
hostd restart as an ASR restart too and pick a moment polyasr is idle.

## Deployed: hold-leak fix on every other facade (91b49f4b backported), 2026-11-03

Each facade stayed on its own release with only 91b49f4b applied, by `node-py/scripts/backport-process-node-holds.py`
(it asserts the shape it edits and refuses anything else). None was moved to main's facade. The new release
dir is `<sha16 of the patched file>` with the base release's `mesh_outbound_py/` copied unchanged. Before
the switch, each patched file was run against a real uvicorn server with the tower's library versions
(starlette 1.10, uvicorn 0.43). The case is an HTTP client leaving mid-response while the facade lock is
held: every base release leaked one hold there and every patched one held none. Each unit was rolled only
when idle (`in_flight 0`, no unit busy). Its unit file or drop-in is backed up as
`<file>.<unit>.before-hold-leak-20261103`. Drop-ins are backed up to `/etc/systemd/system/`, and drama3's to
`~/.config/systemd/user/`.

| unit | base release | new release | switched in |
|---|---|---|---|
| polytts, harmony-deepseek-ocr2 | e2fb4bbace24e144 | 5becb83f6f59ab10 | `<unit>.service.d/zz-harmony-process-node.conf` |
| harmony-glm-ocr, -hunyuan-ocr, -paddleocr-v6, -paddleocr-vl, -perception, -sam21, -sensenova-0 | 3e4360c1bbc9b020 | 31dc6bc1ea08cfcd | same |
| harmony-image | image-wait | 5619d4e8d1e093c4 | same |
| harmony-image-sdxl-depth, harmony-image-flux2-depth | depth-lease | 4ba2ae7f23aa9fe6 | the unit file itself (no drop-in) |
| harmony-polytts-drama3-unconditioned (**user** unit, `systemctl --user`) | admit-wait | d446ef9c9bbbe746 | `~/.config/systemd/user/<unit>.service.d/zz-harmony-process-node.conf` |

- `admit-wait` had been edited in place (`process_node.py.before-engine-route` sits beside it). Its read-only
  `attach()` path, used to poll an existing job, now takes a hold token too.
- `harmony-image-flux2-depth` serves the histo history-scroll inpainting. It was rolled last, at 02:50:05,
  between sections, with `~/histo-scroll/PAUSE` set (the chain stops on a failed request and checks PAUSE
  before each take). PAUSE was removed at 03:02:01, after the hostd switch below.
- After the roll, all 14 facades report `in_flight 0`, every unit `busy: false`, and no worker running.

Roll back one unit: restore its backup, `daemon-reload` (`--user` for drama3), restart it. Restarting a
facade stops its worker.

## Deployed: hostd mesh:// announce is a named 503 (57fbfd61, f5cd4793), 2026-11-03

Release `busy-wait-6190c349-on-admit-fifo-e9edbd61-meshlink-503-f5cd4793` is the busy-wait release, unchanged,
plus those two commits hand-applied: `hostd.py` hunks, `mesh_peer.py`, new `meshlink_runtime.py`,
`tests/test_meshlink_runtime.py`. The busy-wait release itself is recorded by the exclusive-503 change. On the
tower, with hostd's venv, `tests/test_meshlink_runtime.py` passed (7). A TestClient `POST /peers mesh://…` without
`LIVESTACK_RELAY_CAP_KEYS` answered 503 `mesh_relay_unconfigured`. The drop-in is
`livestack-hostd.service.d/zzz-exclusive-device-leases.conf`, backed up as
`/etc/systemd/system/zzz-exclusive-device-leases.conf.hostd.before-busy-wait-meshlink-20261103`. hostd was
restarted once, at 03:01:48 (up 03:01:50), with no lease or image connection open. The process's PYTHONPATH
is the new release, `GET /status` carries `meshlink`, and a mesh:// announce now logs one
`[hostd] POST /peers refused … mesh_relay_unconfigured` line, not a 500 and a traceback.
`LIVESTACK_RELAY_CAP_KEYS` is still unset on this host, so mesh:// peers stay unregistered by design.

## Deployed: a lease holder's work never waits for its own waiters; a gone caller holds no GPU (8deb82f6, dca9910d), 2026-11-03

The incident and the rules are in HARMONY.md, under "A lease holder's work never waits for its own waiters". Two steps:

- **Hotfix, 13:10:17 (tower time):** hostd release `holder-first-8deb82f6-on-busy-wait-meshlink-503`. It is the
  busy-wait + meshlink-503 release plus 8deb82f6's one `hostd.py` hunk. Deployed by
  `~/.local/share/livestack-releases/deploy-holder-first.sh [--rollback]`; the drop-in backup is
  `zzz-exclusive-device-leases.conf.hostd.before-holder-first-*`. This was done mid-deadlock, while an
  abandoned request held both leases, because no quiet moment would come. The stop took 90 s: uvicorn waited
  for open long-polls, then systemd SIGKILLed hostd. Both waiting requests got a 503 and retried. The
  holder's leases survived the restart, and it released them when its job ended (13:21:47). Until the next
  deploy, 19 FLUX jobs from the chain and the zz-tower2 geo flythrough ran back to back, about 22 min each,
  with no gap. The two 503s in that time were callers whose own windows ran out in the line.
- **Full deploy, 21:53:35:** `~/.local/share/livestack-releases/deploy-hangup.sh [--rollback]`. It waited
  for no active lease and no open :8211/:8227/:8766/:8767 connection, with `~/histo-scroll/PAUSE` set from
  20:53 to 21:54. hostd release `caller-gone-on-holder-first` is the holder-first release plus dca9910d's
  `hostd.py`/`hostbroker.py` hunks. Image gateway release `imagegen-hangup-20261103` is
  `imagegen-20261102-426851de` plus dca9910d's `gateway.py`/`client.py`/`transport.py` hunks; one
  `client.py` hunk was applied by hand because that release's docstring differs. Backups:
  `zzz-exclusive-device-leases.conf.hostd.before-hangup-*` and `50-depth-units.conf.gateway.before-hangup-*`.
  polyasr restarted with hostd while idle. Before the switch, the new tests ran against both releases with
  the tower's venvs: gateway 86 passed; hostd all passed except the three boot-id tests, whose feature this
  release predates, and the end-to-end cancel test, whose client half is in the gateway release.
- **Seen live before the full deploy:** the geo flythrough's 4 h `timeout` killed it at 21:06 while its
  request queued. The old gateway still served that request (rows 258/259, 21:09–21:31), a FLUX job for
  nobody.
- **Verified after it:**
  - A FLUX request whose client hung up 40 s into its wait (21:55:53) left no open connection from the
    gateway to hostd, and it never got a lease row, though it was ahead of the next request in line.
  - A second two-device request sent at 21:56:44 overlapped the chain's. It queued, ran after the chain's
    job (rows 264/265, 22:18–22:40) and returned 200 with lease evidence in profile order. The chain's next
    request took over at 22:40:04.
  - Every request now takes `4bac2869` first (sorted order).

Roll back: run `deploy-hangup.sh --rollback` (back to the holder-first hostd and the old gateway), then
`deploy-holder-first.sh --rollback`.

## Deployed: busy tenant through a drain, boot-id store, heartbeat that rides out a stall (41f7cb4d, 96d0943e, 5f977882), 2026-11-04

These fix the morning's four 503s; see HARMONY.md, "Four 503s in one morning".

- hostd release `busy-tenant-bootid-on-caller-gone`: `caller-gone-on-holder-first` plus 41f7cb4d's `hostbroker.py` hunk and 96d0943e's `device_leases.py` (the boot-id store, previously recorded above as pending). Image gateway release `imagegen-heartbeat-20261104`: `imagegen-hangup-20261103` plus 5f977882's `client.py` hunk.
- Deployed by `~/.local/share/livestack-releases/deploy-busy-tenant.sh [--rollback]` at 10:57:08 (tower time). The script waited for no active lease and no open :8211/:8227/:8766/:8767 connection; `~/histo-scroll/PAUSE` was set from 10:52 to 10:57. Drop-in backups: `*.before-busy-tenant-*`. polyasr restarted with hostd.
- Before the switch, the tests ran on the tower with its venvs. hostd: 42 passed, including every boot-id test and the new busy-tenant test. The one failure was the end-to-end cancel test, whose client half is in the gateway release. Gateway: 87 passed, including the new heartbeat test.
- After the switch, the chain's next take (10:58) holds both leases (rows 319/320), stamped with the tower's real boot id `7b2eab2d-…`. A same-boot open of the store by any main-code process is now harmless.

Roll back: `deploy-busy-tenant.sh --rollback` (back to `caller-gone-on-holder-first` and `imagegen-hangup-20261103`).

## Deployed: stale-resident grants (6f5f19fe), 2026-11-06

**Symptom:** qwen text-to-image requests got 503 "Harmony grant did not warm this provider".

**Cause:** at 12:45:28 (tower time) hostd evicted `qwen_image_2512` to make room for drama3. The harmony-image facade finished the job it was running and stopped the worker at 12:45:32. hostd's snapshots in between still saw qwen resident, and hostd remembers a resident unit for 90 s (`residency_grace_s`). So from 12:45:38 to 12:46:37 it granted eight qwen icon requests as "resident" without dispatching a warm (decision ledger: `grant … resident; measured free 25 GB`), and the facade refused each one. 6f5f19fe was already on main; this deploy puts it on the tower.

**Releases:**
- hostd: `stale-resident-6f5f19fe-on-device-busy`, which is `device-busy-dd5e748c` plus 6f5f19fe's `hostbroker.py` hunks (`authoritative_absent`, `absent=`).
- harmony-image facade: `c1761ed3ec8f29b5`, which is `5619d4e8d1e093c4` after `node-py/scripts/backport-process-node-stale-resident.py`. The residence report now says `residence_authority: owner`, and `begin()` warms under a stale grant. `mesh_outbound_py/` was copied unchanged.

**Tests, run on the tower with its venvs:**
- 6f5f19fe's hostbroker test fails on the old release and passes on the new one (95/96; the one failure is the device-hold test, a feature this release predates).
- Its two facade tests, run against each facade file, fail on the old file and pass on the new one. The two other failures fail on both files; they test features these releases predate.

**Deploy:** `~/.local/share/livestack-releases/deploy-stale-resident.sh [--rollback]` at 13:13:05. It waits for no active lease, no open :8210/:8211 connection, and nothing in flight on polyasr or drama3; it reads `load.in_flight` there, because the speech router keeps idle keep-alive connections. `~/histo-scroll/PAUSE` was set from 13:12 to 13:13. The backups are `*.before-stale-resident-*` in `/etc/systemd/system/`. After the restart, `GET :8210/livestack/residence` carries `residence_authority: owner`. The other facades are unchanged; the hostd half alone stops the stale grants for them once they report owner authority, which they do not yet.

## Deployed: a cut image request stops its GPU work before its leases go (commit bd05436d), 2026-11-07

On xc-tower-ubuntu at 09:08:38 (FLUX facade) and 09:08:58 (gateway) EST, each hand-applied onto the release it ran:

- Image gateway: `imagegen-principals-20261107-cancel-bd05436d` (= `imagegen-principals-20261107` + bd05436d's
  `gateway.py`), drop-in `harmony-image-gateway.service.d/70-cut-holds-leases.conf`.
- FLUX.2 facade: process-node release `31f56b6053018fe7` (= `4ba2ae7f23aa9fe6` + bd05436d's proxy change, applied
  by hand because that release predates main's `begin`), drop-in `harmony-image-flux2-depth.service.d/70-cut-reaches-worker.conf`
  overriding `ExecStart`.
- FLUX.2 worker: `imagegen-20261102-724ec228-cancel-bd05436d` (= `imagegen-20261102-724ec228` + bd05436d's
  `worker.py`, `runtime.py`, `flux2.py`, `sdxl.py`), set as `PYTHONPATH` in
  `~/.local/share/harmony-process-node/harmony-image-flux2-depth.json` (backup `.before-cancel-bd05436d`).

The imagegen tests pass against the gateway and worker releases. The facade release passes the new cut test, where
the old release fails it, and fails nothing else that the old release passes. `stop_when` gives the tower's
VideoX-Fun `Flux2ControlPipeline` its `callback_on_step_end`. Script:
`~/.local/share/livestack-releases/deploy-image-cancel.sh [--rollback]`. It waits for no active lease and no open
:8211/:8217 request, and restarts the FLUX facade (which also ends its worker) and then the gateway. The SDXL depth
unit is unchanged.
