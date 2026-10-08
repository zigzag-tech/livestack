# Design: Private temporary storage for supervised attempts

`SystemdExecutor.start` adds `PrivateTmp=yes` to each transient attempt unit. This isolates both `/tmp` and `/var/tmp` for the process tree while preserving the current unit's `NoNewPrivileges` setting, resource limits, delegated cgroup behavior, bind paths, and inaccessible paths.

The systemd unit owns the temporary mount namespace for exactly the attempt lifetime. The existing attempt ID still names the unit; `WorkerJournal` continues to persist only the active attempt identity and uses the unit/cgroup to reconcile cleanup. No new durable state or identifier is introduced. The wrapper's execution descriptor, logs, and artifacts remain under the existing attempt output directory, and source/environment mounts continue to use their explicit bind paths.

The integration test uses a real systemd user unit with `NoNewPrivileges=yes`. Its child verifies `NoNewPrivs: 1`, creates a marker beneath `/tmp/.X11-unix`, and exits. The test verifies the marker is absent from the host namespace and the attempt unit reports private temporary storage. This proves the needed writable path without granting privilege or changing host files.

Ledger obligation: no placement or policy decision changes, and no new ledger record is required. Existing job, attempt, and completion identities remain unchanged; the test checks only process isolation and cleanup.
