# Exclusive-device lease specification

## ADDED Requirements

### Requirement: Durable exclusive acquisition

The broker SHALL persist exclusive requests and support `draining`, `exclusive`, `expired_draining`, `released`, `failed`, and `timeout` states. One active request per device and idempotent request IDs SHALL be enforced. Active records SHALL survive broker restart.

#### Scenario: Idempotent exclusive grant

- **WHEN** an eligible caller repeats the same request ID and owner
- **THEN** the broker returns the existing outcome without duplicate eviction or a second lease

#### Scenario: Active lease survives restart

- **WHEN** the broker reopens the store with an active exclusive record
- **THEN** the device remains fenced and the same owner may heartbeat or release it

### Requirement: Managed drain and measured verification

Acquisition SHALL fence first, evict only idle non-hard-pinned managed tenants, reclaim allocators, and verify fresh measured free memory before checkout. It SHALL NOT evict busy or hard-pinned tenants, abort in-flight work, or kill external processes.

#### Scenario: Measured free memory is insufficient

- **WHEN** managed placements are gone but measured free memory is below `min_free_bytes` or unavailable
- **THEN** acquisition returns an external/memory blocker and no lease

#### Scenario: Busy or pinned tenant blocks

- **WHEN** a tenant is busy, hard-pinned, within its residency floor, or in flight
- **THEN** acquisition names the blocker and does not evict it

### Requirement: Exclusive fencing and owner access

Ordinary admission and warm/restore SHALL be fenced during active drains and other owners’ exclusive leases. Only an admission carrying the matching active `device_lease_id` MAY use the device during exclusivity.

#### Scenario: Other caller is refused

- **WHEN** another caller requests admission during an active exclusive lease
- **THEN** the request is refused without a degraded grant

#### Scenario: Release restores shared scheduling

- **WHEN** the valid owner releases after stopping GPU work
- **THEN** the device returns to shared scheduling and later ordinary admission may proceed

### Requirement: Expiry cleanup and fail-closed behavior

Heartbeat expiry SHALL enter `expired_draining` and retain the fence until managed cleanup and quiescence. Exclusive acquisition and owner-checked admission SHALL NOT use degraded grants.

#### Scenario: Expiry does not imply job completion

- **WHEN** a lease expires while managed work remains busy
- **THEN** the device remains fenced as `expired_draining` and no other owner is admitted

### Requirement: Multi-device lease admission

A worker spanning multiple devices SHALL hold a distinct active lease for every configured device. The gateway SHALL acquire all leases before admission and release already-acquired leases if a later acquisition or generation fails. The broker SHALL validate every ordered `device_lease_ids` entry for the authenticated owner and SHALL use only the first lease's device for worker placement.

#### Scenario: Secondary lease is invalid

- **WHEN** any secondary lease is missing, expired, duplicated, assigned to another owner, or inconsistent with its device
- **THEN** admission is refused without a degraded grant or model loading

#### Scenario: Device without a model facade

- **WHEN** a local CUDA device has no model-server peer
- **THEN** a configured broker-local meter SHALL supply its measured capacity and free memory before exclusive checkout

#### Scenario: Result evidence

- **WHEN** a multi-device generation completes
- **THEN** the worker and gateway evidence SHALL pair every lease ID with its configured device and preserve the primary fleet/local grants

### Requirement: Client and operational evidence

The client SHALL provide acquisition, heartbeat, release, and visible lease-loss behavior without polling storms or degraded exclusive grants. Fleet/status views SHALL expose active states and blockers.

#### Scenario: Heartbeat loss is visible

- **WHEN** an exclusive heartbeat is unknown, expired, or fails
- **THEN** the client surfaces lease loss and releases any remaining claim in `finally`
