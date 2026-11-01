# Add exclusive-device leases

## Why

`Unit.exclusive_device` controls model co-residency but does not let one caller drain a GPU, verify measured free memory, and retain ownership across broker restart. Image-generation experiments repeatedly OOMed because grants were memory-budget decisions rather than exclusive device leases.

## What changes

- Add durable `draining → exclusive → released` device leases with `expired_draining` cleanup and terminal failure/timeout states.
- Extend `/admit` with opt-in exclusive acquisition, explicit device/free-memory requirements, bounded waits, and idempotent responses.
- Fence ordinary admission and warm/restore work during drains and other owners’ exclusive leases.
- Evict idle managed tenants, reclaim allocators, and verify fresh measured free memory before checkout. Busy, pinned, young, in-flight, unavailable-peer, and external-memory blockers are named.
- Add owner-checked `device_lease_id` admission plus heartbeat/release and a non-degrading client context manager.

## Boundaries

The guarantee covers participating managed tenants and valid leaseholders. External processes are never killed or claimed to be managed; their memory can block acquisition. Direct node warm/ensure calls are outside the supported admission-wrapped contract. No production broker, worker, or deployed configuration is activated or changed.
