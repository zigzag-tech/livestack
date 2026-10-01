## Why

Benchday's `compilation-requires-harmony-admission` requires an upstream
authority contract. `_plans/durable-workloads.md` describes resource admission
and installed handlers, but it does not constrain compilation with operator
physical-host policy or authenticate a compiler launch's process containment.

## What Changes

- Versioned, bounded operator policy maps authenticated host enrollment aliases
  to physical identities and permits specific compilation classes per identity.
- Placement intersects installed handlers and resource admission with policy;
  renewal and worker-local verification fail closed on revocation/expiry.
- Worker-local Unix socket verification checks peer process containment and live
  fenced attempt identity, rather than trusting environment metadata.
- Owned native/compiler and rootless Docker children retain existing supervision.

## Capabilities

### New Capabilities

- `compilation-authorization`: operator compilation eligibility and authenticated
  supervised launch verification.

### Modified Capabilities

None.

## Impact

Workload authority configuration, placement, worker supervision, HTTP control,
and consumer library/CLI. Benchday caller migration and remote-principal OS
confinement belong to its companion change. No unreserved fallback is added.
