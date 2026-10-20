# Offline native TTS crash reconciliation

This Linux owner utility defaults to **dry-run**. It is for one already-dead user-service incarnation with an authoritative same-PID/same-boot/same-invocation CUDA OOM journal witness. It refuses live/reused PIDs, live/missing-witness cgroups, changed config/request/receipt, unexpected artifacts, external reservations and concurrent owner locks. It does not start services, submit inference, kill work or release external leases.

`jobs.py` records intermediate admission in SQLite while `receipt.json` remains its exact accepted nine-field snapshot. Recovery compares those specific original fields, the complete request digest and exact caller-pinned disk bytes. It does not normalize arbitrary mismatches.

The terminal failed receipt distinguishes kernel-observed owner death from the lost executor/CUDA synchronization callback: `executorBarrierPassed:false`, `normalCompletion:false`, `localLeaseReleased:false`, `localLeaseTerminatedWithOwner:true`. The original local lease lived in the dead process; broker admission must explicitly have returned `lease_id:null`. No completed audio/native take is reconstructed.

Required flags: `--config PATH --config-sha256 SHA --job-id ID --request-sha256 SHA --receipt-sha256 SHA --unit NAME.service --invocation-id ID --evidence NEW_PATH`. Omit `--apply` for the read-only SQL dry-run. Explicit `--apply` additionally holds a SQLite immediate transaction and publishes a recovery intent before a complete old-row CAS. The worker owner lock remains held until completion. Existing request/receipt/SQLite evidence must be copied and hashed before a reviewed apply.

Apply commits the terminal row and then atomically publishes its disk receipt; these two stores are not one distributed transaction. The durable external intent preserves the proposed exact receipt if interruption leaves them different. A failed/unknown apply must be inspected, never blindly repeated. The utility refuses its original running-row precondition after a committed CAS. Review and reconcile any partial publication explicitly.

Scope: source import and one actual read-only dry-run only until separately reviewed exact apply. No unit suites, compiler, restart or model effect are implied. Native context is fixed at session creation; a lower output frame budget limits generation length, not the fixed initial KV capacity. Smaller contiguous thought groups can bound full-waveform decoder work but are not yet a measured OOM cure.
