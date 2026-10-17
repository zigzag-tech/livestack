# Publish identity facts through authenticated source streams

## Why

The fleet broker and workload authority already own complete, authenticated `hosted_on` Fact v1 cuts, but the Hub obtains them by HTTP polling. Both processes already have native Benchday stream producers. Moving the cuts to separate source-owned streams removes the extra polling transport and lets the Hub observe registration, refusal, and stream loss as named outcomes.

## What changes

- Publish fleet-node and workload-worker facts through separate account-scoped, one-per-realm host-service state contracts.
- Reuse each process's existing `IdentitySnapshotPublisher`; page one complete cut with a shared inner generation and sequence.
- Bound each cut to 4,096 Facts and 2 MiB of canonical snapshot JSON, each stream partial to 24 KiB, and each cut to 128 pages.
- Refresh within 15 seconds against the existing 60-second Fact freshness limit.
- Require explicit configured authority ids and an authenticated Hub authority link; do not fall back to hostname identity.
- Keep fleet-node publication on the fleet broker only and worker publication on the workload authority only.

## Non-goals

- Do not change placement, dispatch, workload admission, principal ownership, or decision-ledger behavior.
- Do not publish these Facts in `harmony.fleet/1`, `harmony.workload/1`, or a job/intent contract.
- Do not publish an incomplete cut after a source read, bound, or ingress failure.
- Do not add HTTP fallback to Benchday. The existing HTTP routes remain only while their consumers are removed and are deleted if repository-wide usage confirms they are unused.

## Affected capability

Add `identity-facts-streams`, building on the archived `fleet-identity-facts` source model and coordinated with Benchday's `hosted-identity-facts-streams` change.
