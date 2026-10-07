## Why

The workload authority owns every job-input object: `BlobStore` is a directory on the authority host,
built directly in `service.py` / `http.py`, and `PUT|GET objects/<digest>` is the only place bytes
enter or leave (`object_routes.py`). Placement, retention, quota and reference tracking all assume
that. So a publisher and a worker that share a machine still move every byte through the authority
host, and the authority is a single disk, a single quota and a single outage for the whole fleet.

Long-term the authority cannot own everything. It should own the **catalog** (digest, size, owner,
who holds it) and the **decisions** (which holder serves this transfer, which worker runs this job),
and leave bytes with **holders**. The authority host's own disk becomes one holder among many.
Where publisher, holder and worker are close (the same machine, the extreme case), routing must
choose that path *because it is cheapest*, from declared locality and measured cost, with no host,
app or region named in the core.

Design records this realises: `_plans/fleet-broker.md` (host brokers are the sole residency
authorities; the same stance applied to bytes) and `_plans/decision-ledger.md` (every routing
decision leaves a record). Neither describes object storage, so nothing in them is stale; both
gain a section (task 5.2).

## What Changes

- **Holders.** A holder is a registered content store with identity, address, declared locality,
  capacity and retention policy. The authority's `BlobStore` is registered as the built-in holder
  `authority`; existing behaviour is exactly "all objects held by `authority`".
- **Catalog.** The authority records `(owner, digest, size, holders[])`. Reference tracking and
  prune decisions become per holder; the authority prunes only objects it holds.
- **Claims.** An upload grant may name a holder. The publisher writes to that holder; the holder
  verifies the digest and reports a claim; the authority records the holder only after
  verification. Bytes need not touch the authority.
- **Holder routing.** `choose_holder(request, catalog, measurements, policy)` is one pure function
  returning the holder and a reason. Inputs: declared locality of publisher / holder / (for reads)
  worker; measured transfer cost between them, with its age; free capacity; principal policy
  (allowed holders); health. Unknown measurements are unknown, never zero; with no information it
  returns `authority`.
- **Fetch by locator.** The catalog answers `GET objects/<digest>` with the holder list in cost
  order; workers fetch through the existing `RouteSet` using a new `holder` route kind and a
  short-lived, attempt-bound credential.
- **Locality-aware placement.** Job placement derives `locality_host` (an existing job field fed to
  the pure scheduler) from where the job's inputs are held, so the worker next to the bytes wins.
- **Ledger.** Every holder choice and every placement locality hint is recorded with its reason.

Not changed: job, attempt and fence semantics; worker handler contracts; the single-request PUT and
the resumable upload; the mirror options (they remain best-effort caches); any consumer's selectors.

## Capabilities

### New Capabilities
- `object-holders`: holder registry, catalog, claims, holder routing, fetch by locator, locality-aware placement.

### Modified Capabilities
- `workload-upload-grants`: a grant may name a holder (additive; omitted means `authority`).
- `transfer-routes`: gains the `holder` route kind (its follow-up 4.4).

## Impact

`node-py/livestack_node/workloads/{blobs,object_routes,transfer,routes,route_kinds,placement,service,http,model,store}.py`,
a new `holders.py` (registry, catalog, `choose_holder`), `schema.sql` (catalog tables, additive),
`node-py/tests/test_workload_holders.py`. Old workers keep working (the authority is always a
holder and always answers `GET objects/<digest>`); a worker needs a release to use other holders.
Rollout is operator-run, authority first, and is a separate step.
