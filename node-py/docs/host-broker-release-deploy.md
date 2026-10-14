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
