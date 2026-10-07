## Direction (read this first)

The authority is a **coordinator of content, not the owner of it**. It decides and records; holders
store and serve. The `authority` holder keeps today's behaviour so every existing caller is
unaffected, and the rest is additive. Nothing in the core names an application, a host or a region;
locality is *declared by each node* and cost is *measured*. The same shape as `transfer-routes`
(declared descriptors, evidence second) and the placement scheduler (pure function, ledger record).

## State ownership (which process owns what)

| State | Owner | How ids cross |
|---|---|---|
| Catalog rows `(owner, digest, size)` and `holdings(digest, holder, verified_at)` | authority store (SQLite, `schema.sql`) | digest is the id everywhere |
| Holder registry (id, address, locality, caps, policy) | authority, loaded from schema-validated config; a holder also announces itself | `holder_id` |
| The bytes, per-holder retention and quota | the holder | holder reports `{digest,size}`; authority never infers |
| Locality declaration | each node's own config (`host_id`, `segment`) | carried in registration |
| Transfer measurements | each client's `RouteSet` health (per route per peer), reported to the authority as evidence with a timestamp | `(holder, peer)` |
| Job `locality_host` | derived by placement at admission from the catalog | existing job field |

## Decisions

1. **Authority is a holder, not a special case.** `BlobStore` is wrapped as holder `authority`
   (`kind=local_fs`). The catalog is the source of truth for "who has this digest"; the `blobs`
   table remains its authority-holder detail. A migration backfills one holding per existing blob.
2. **Claim, then record.** Publisher calls the authority for a grant that names `holder` (default
   `authority`). The holder verifies the whole digest and size before reporting the claim, over the
   holder's own credential; the authority records the holding only on a verified report. An
   unverified or oversized claim leaves no holding. Idempotent per `(digest, holder)`.
3. **Holder protocol is small.** `PUT|GET objects/<digest>` (the same wire as the authority, so the
   existing `http` route kind and the resumable upload work unchanged), plus `GET /holder/status`
   (capacity, free bytes, health). A holder is therefore implementable as a small process next to a
   worker; the authority's own routes are the reference implementation.
4. **Routing is a pure function.** `choose_holder` ranks eligible holders by, in order: eligible
   (health, principal policy, free bytes), declared locality tier (same host < same segment < same
   mesh < other), measured cost where fresh, then config order. Result carries a reason string
   (`"holder zz-tower2: same host as publisher"`). Stale or absent measurements fall back to
   locality tier; absent locality falls back to `authority`. Unknown is never treated as zero.
5. **Reads follow the catalog.** The authority answers a worker's fetch with holders in cost order
   for that worker; the worker's `RouteSet` tries them with failover and resume as for any route.
   The authority remains a holder of last resort only if it actually holds the bytes. The fetch
   credential is attempt-bound and expiring, minted by the authority, verified by the holder
   (HMAC over `(attempt, fence, digest, holder, expiry)`; the holder holds a verifier key, never
   the authority's secrets).
6. **Fail early, not mid-job.** Admission checks that every job input has at least one live holder
   reachable from some eligible worker; otherwise the job waits with reason `inputs_unavailable`
   and the ledger says which digest. A holder lost after admission fails the transfer via the normal
   RouteSet failover; if all holders fail, the error carries the trail.
7. **Retention is the holder's.** The authority prunes only what it holds. For other holders it
   records `retain_until` as a request; the holder may keep longer. A digest referenced by a durable
   job is never pruned on the authority holder (as today); for other holders the reference is
   advertised so a cooperative holder refuses to prune it.
8. **Locality-aware placement.** At admission, `locality_host` is set from the holder that has the
   most input bytes, unless the spec sets it. It is a preference (existing semantics), so a busy
   preferred host does not block the job.
9. **Replication is optional and explicit.** A grant may name `replicate_to`; the second holder
   fetches from the first over the same protocol. It replaces nothing today (the mirror options
   remain best-effort caches).
10. **Evidence.** Holder choice and locality hint are ledger records; `holders.counters` and a
    `GET /v1/holders` view (like the fleet resource map) show holders, holdings and last choice.

## Risks

- Holder lost or pruned: the job fails to transfer, not to schedule, if catalog is stale; a verify-on-
  read digest check and a periodic holder reconciliation (catalog vs `GET /holder/status` listing)
  bound the staleness. Admission check in (6) catches the common case.
- Catalog and holder disagree on size: the claim is refused and ledgered.
- A rogue holder: it cannot create catalog entries it did not verify (the claim must match a grant
  the authority issued for that digest); it can serve wrong bytes, which the digest check rejects.
- Added indirection for the common case: with no extra holders registered the code path is the
  existing one plus one catalog lookup.
- Two content mechanisms (unchain `http-pull`, `storage-*`): they plug in as holder kinds / route
  kinds rather than a third bus. Task 4.1 records which one backs a remote holder.

## Migration and parallel running

Additive schema; the `authority` holder is always present; old workers and publishers are
unchanged. A deployment is "parallel" in the strong sense: nothing is switched for an app until its
principal is given an allowed non-`authority` holder, and removing that grant returns it to the old
path with no data movement.
