# Worker releases: build, verify, roll

Status: operational runbook (2026-10-06). Replaces hand-assembled `cp -a` overlays.

## What a release is

The directory a worker unit puts on `PYTHONPATH`: `<release>/node-py/{LICENSE,livestack_node/**}`
(repo-root `LICENSE`, no tests, no `__pycache__`) plus `node-py/RELEASE.json` (commit and content
hash). It is built from one git commit, never edited in place, and named for it:
`livestack-<sha8>` (the legacy `native-route-...+transfer8g+...` names are retired).

The worker needs only a stock `python3` on `PYTHONPATH=<release>/node-py`: no third-party
package is imported on its startup path (enforced by a test). Release `livestack-70a11344`
alone required pydantic (hosts there carry a `deps-pydantic-*` directory on `PYTHONPATH`);
releases from a later commit do not, and the extra path entry is then harmless.

## Build and verify (any machine with the repo; this does not touch a worker)

    node-py/scripts/build-worker-release.py build <commit> --out /tmp/rel/livestack-<sha8>
    node-py/scripts/build-worker-release.py hash  /tmp/rel/livestack-<sha8>
    node-py/scripts/build-worker-release.py verify BUILT DEPLOYED [--ssh HOST] [--commit origin/main]

`verify` compares per-file sha256 (modes/mtimes ignored), lists files only in one side or changed,
and with `--commit REF` names the commit on REF that holds each deployed copy, or flags
**HAND-EDIT** when none does. Two trees with the same content hash are the same release.

Result of the first run (2026-10-06): the deployed zz-joe release
`native-route-bbf4d538+hostledger224dce34+keepalivead4f0a45+transfer8g+handlerregistrycc8597a7+toolarge9c89f222`
is NOT main HEAD. It is an older mainline tree (every other file equals an ancestor commit on
origin/main; 7e951ad4 is the base; bbf4d538, 224dce34, ad4f0a45, 9c89f222 are ancestors of main;
cc8597a7 is the pre-rebase copy of main's b432e8bf). Only `workloads/worker.py`, `model.py` and `client.py`
were hand-assembled (9c89f222's hunks and `transfer_byte_limit()` laid over an older base).
Main now carries everything functional in them, including `transfer_max_bytes` (same key and the same
log line `object transfer ceiling is N bytes`), so a release built from main is a superset. Main also has
platform (macOS/Windows) and task-environment code the deployed release lacks.

## `transfer_max_bytes`

`worker.json` key, integer 1 .. 8 GiB (the authority object bound), default 2 GiB, applied to every
upload and download of that worker. A handler's `output_max_bytes` still overrides it for that
handler's result artifacts. Workers that move image archives (docker save ~3.6 GiB) set
`"transfer_max_bytes": 8589934592`. Config keys survive a release roll; check `worker*.json` has it
BEFORE the roll (`grep transfer_max_bytes ~/.config/livestack-workloads/worker*.json`) and that the
journal shows `object transfer ceiling is 8589934592 bytes` after.

## Drain and idle rule

Never restart the authority. To stop a worker taking new work: set `"claim_enabled": false` on its
principal in the authority config and reload it (`systemctl --user reload livestack-workload-authority` = SIGHUP; principals reload live, see
`authority-principal-reload.md`). Roll when the worker has NO running attempt (`workload ls`/authority
view; an idle worker needs no drain), restart only that worker's unit, then set `claim_enabled` back
and SIGHUP again. Jobs on other workers are unaffected; the worker unit restart recovers/reconciles
its own state. In practice a quiet gap on the publishing worker rarely appears: do not wait for one,
drain it (claim_enabled false), let the running attempt finish (never kill it), roll, re-enable. Never
roll while `publish.sh` is running a stage on that worker.

## Per-class steps

Common: build + `verify` (sha must match what you stage), stage under a NEW directory name, point the
unit at it, restart, confirm the journal line and one probe job, keep the previous release dir for
rollback (point the unit back, restart).

- zz-joe e2e-1/e2e-2 (`livestack-workload-worker{,-2}.service`, user units): copy release to
  `~/.local/share/livestack-workload-releases/livestack-<sha8>`; edit the highest-numbered drop-in that
  sets `PYTHONPATH` (`70-handler-release-registry.conf`, then remove superseded 30/50/60 PYTHONPATH
  lines, they are overridden anyway); `systemctl --user daemon-reload && systemctl --user restart <unit>`.
- zz-joe-release (`livestack-workload-worker-release.service`, still `...+transfer8g`): same, but ONLY
  after draining as above; it carries publishes. Verify the stage is idle first.
- xc-win-1-wsl (lacks transfer8g and 9c89f222): same as zz-joe on the WSL unit; add
  `transfer_max_bytes` to its worker.json first. The native Windows worker follows `windows-worker.md`
  (stage under `C:\harmony\releases\livestack-<sha8>`, never in place).
- xc-tower-stager: same as zz-joe for its unit; add `transfer_max_bytes` if it moves images.
- Mac Lima guest (xc-mac-studio-harmony): inside the guest as zz-joe (Linux systemd); the macOS
  host-pressure worker is separate and unchanged.
- Checkout-based hosts (zz-tower0 and others running from `~/livestack`): `git -C ~/livestack pull
  --ff-only origin main` then restart the worker unit. The checkout IS the release, so drain first and
  restart only when idle; prefer switching them to a built release dir for reproducibility.
