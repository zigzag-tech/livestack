# Disposable authority-release rollback branch check

Run at approximately 2026-10-11 01:56 UTC on lappy-bellinzona. The exact
Livestack deployment script was copied into a temporary fixture; its SHA-256
was 4458f3a5b26a5c91f96fd0f38acea14aebc2d1f6563b65d1329156259d607e6f.
The fixture used a temporary HOME with a disposable SQLite database, dummy
authority config, and a previous-release drop-in. A PATH shim made the first
systemctl restart fail and the rollback restart succeed.

Observed results:

- The script performed the database backup and its integrity check passed.
- It entered the rollback branch after the deliberately failed first restart.
- It restored the previous drop-in and authority config byte-for-byte.
- It reloaded systemd, retried the restart, and the shim reported the unit
  active. The script exited 1, as expected for an aborted deployment.
- The recorded calls were daemon-reload, failed restart, daemon-reload,
  successful rollback restart, and is-active.

The candidate-release checker was a passing stub in this drill. Candidate
validation is separate and passed during the operator's real activation
prechecks. No production unit, config, database, worker, or authority state was
touched. This verifies the deployment script's restore branch, not a live
rollback or end-to-end recovery of the active authority. The production
rollback acceptance remains open.
