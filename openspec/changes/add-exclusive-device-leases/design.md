# Design: exclusive-device leases

## Authority and persistence

`HostBroker` coordinates the lifecycle. A small stdlib SQLite store in `device_leases.py` persists one request per idempotency ID under `LIVESTACK_LEDGER_DIR/device-exclusivity.sqlite3`; isolated runs use isolated directories. One active request per device is allowed. Reopening the store preserves active ownership and resumes drains.

States are `draining`, `exclusive`, `expired_draining`, `released`, `failed`, and `timeout`. Explicit release returns to shared scheduling. Expiry becomes `expired_draining`, not shared: managed remnants and busy work still fence the device until cleanup. Expiry is not claimed to stop a caller’s job.

## Acquisition

1. Validate dispatching broker, local non-hosted device, owner, idempotency key, and positive `min_free_bytes`; persist `draining` before eviction.
2. Snapshot fresh placements, busy/pin/floor/in-flight state, and measured memory. Evict every idle non-hard-pinned managed tenant and reclaim peer allocators. Busy, hard-pin, residency-floor, in-flight, peer, and other-owner blockers are returned without a lease.
3. Re-snapshot after eviction/reclaim. Require no managed placements/in-flight loads and `measured_free >= min_free_bytes`. Missing/insufficient measurements block. Only then persist `exclusive` and issue a mandatory lease ID.
4. Bounded waits use a condition variable with release/expiry/refresh notification and slow safety wake, not tight replanning. Acquisition and reconcile may race without holding locks during peer HTTP; the store's draining-to-exclusive transition is idempotent so a stale driver returns the already-issued lease instead of failing.

The planner receives immutable device lease state. Ordinary demand and warm/restore are unavailable during `draining`, `exclusive`, or `expired_draining`, except for an admission carrying the matching active `device_lease_id`. Static `Unit.exclusive_device` behavior is preserved.

## HTTP and client

`POST /admit` accepts `exclusive_device`, `selector.device_id`, `min_free_bytes`, optional `wait_timeout_s`, and idempotency `id`. It returns verified free bytes and a mandatory lease ID or `granted: false` with structured blockers. Exclusive acquisition and owner-checked admission never use degraded grants.

Existing heartbeat/release routes support exclusive IDs without changing hosted IDs. `client.admit()` forwards options and `device_lease_id`; `exclusive_lease()` handles acquisition, heartbeat loss, and release in `finally` without silent fallback. Legacy request bodies and behavior are unchanged.

## Multi-device workers

The gateway owns an ordered set of exclusive contexts, one per configured physical device, using an `ExitStack` so partial acquisition is unwound. It sends ordered `device_lease_ids` to the broker and as JSON in the worker's authenticated lease header. The broker validates every lease against its own device and the authenticated owner, but derives only one selector from the first lease: there is one worker placement, not one placement per GPU. Worker configuration charges only `resident_bytes` to that primary placement; secondary ControlNet weight bytes are not added to it.

`LIVESTACK_DEVICE_METERS` maps device IDs to logical CUDA indices for broker-local meter peers when no model facade exists on a secondary device. Ordered `device_ids` and `CUDA_VISIBLE_DEVICES` define the physical/logical mapping. Diffusers transformer budgets and `controlnet_device` use logical indices; a transformer forward wrapper and per-block hooks move each ControlNet residual to that block's output device before addition. Results retain every lease/device pair, the primary grants, actual offload maps, and per-device memory peaks.

## Failure and restart

Eviction or peer faults preserve the drain record and error; they never imply an empty device. Restart reloads active records and continues reconciliation. Reconcile runs before ordinary warm/restore planning. Fleet/status projections expose active state and blockers without hiding unknown peers or memory.

Direct node warm/ensure is not a cross-process hard fence. The supported contract is admission-wrapped work. Production activation and a later image-generation retry are separate steps.
