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
