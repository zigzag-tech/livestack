## Context

See `proposal.md` for the source-publisher use case. `_plans/durable-workloads.md` already makes the workload owner's CAS the canonical input store and keeps transfer separate from job admission. Its current caller-token assumption is stale for a source publisher that must upload input bytes without being authorized to create jobs. The live authority is Python `WorkloadServer` plus `WorkloadStore`/SQLite and `BlobStore`; object ownership is currently the authenticated principal id.

## Goals / Non-Goals

**Goals:**

- Delegate one digest- and size-bound object upload into the authenticated workload owner's existing CAS namespace.
- Make lost responses and process restarts observable through durable status metadata, without a second object transfer where the CAS write completed.
- Keep the existing `/objects/<digest>` caller and fenced-worker protocol unchanged.
- Give the ZZOPS service a narrow way to mint a transfer authorization while keeping its normal workload credential off the publisher.

**Non-Goals:**

- Changing worker downloads, job submission, placement, artifact downloads, or the object retention policy.
- Selecting regional relays or COS routes. The grant returns the authority's current direct upload URL; transfer-broker routing can later supply a different destination under the same bound grant contract.
- Moving transfer bytes through ZZOPS or adding a second object store.

## Decisions

### One configured caller may delegate only into its own object namespace

Add an opt-in `upload_grants: true` principal setting, default false. `POST /v1/workloads/upload-grants` uses that principal's ordinary caller token and accepts only `request_id`, `digest`, `size`, and bounded `expires_in_seconds`; it does not accept an owner field. The server binds the grant owner to `principal.id`. This lets the ZZOPS dispatcher principal authorize input for its own future jobs while the source publisher receives no caller token.

Alternative considered: add a `publisher` role with a long-lived token. That would either place the object under a different owner, which jobs cannot read, or require a permanent cross-owner permission. A per-object grant avoids both.

### Use an opaque bearer capability and one direct object route

The mint response carries a random 256-bit capability, grant id, expiry and absolute upload URL. The authority stores only the capability's SHA-256 verifier. The publisher sends the bytes directly to `PUT /v1/workloads/upload-grants/<grant-id>/objects/<digest>` using the capability as a bearer credential. The request must include the exact `Content-Length`; chunked transfer is refused. Only this path recognizes the capability. Regular principal authentication and all normal routes remain unchanged.

An in-process per-grant lock prevents two simultaneous PUTs or a grant rotation from racing one upload. `BlobStore.put(owner, digest, size, stream)` remains the only byte writer, so existing digest verification, private staging, owner index and quota checks remain authoritative. The lock is not durable state: process death closes the socket and BlobStore recovery removes its incomplete staging file.

Alternative considered: expose the regular caller token to the publisher. It is rejected because a publisher could submit a job or access existing owner objects. A signed query URL is also rejected because URLs are commonly copied into access logs; the capability stays in an authorization header, which request logging already excludes.

### Persist grant identity and receipt in the workload authority database

Add bounded `upload_grants` rows in the same SQLite database as jobs and blob ownership. The durable identity is `(owner, request_id)`; each row records `grant_id`, owner, request id, digest, size, verifier, created/expiry times, and state. A matching repeated POST returns an uploaded receipt if the object is already owned and verified; otherwise it revokes a previous inactive capability and mints a new one. An active PUT blocks rotation. A changed digest or size under the same request id is refused.

The publisher can query status by request id with its ordinary workload owner token. If the authority crashed after CAS commit but before marking the grant uploaded, status checks the durable CAS owner/digest/size row and completes the receipt without reading the object body. Status never returns a capability. ZZOPS stores the returned grant id and reported receipt in its pending source record and verifies that status still names the pending app publication's digest and size before promotion.

The grant table is capped at 4,096 rows and 512 unexpired grants per owner. Expired or terminal rows older than 24 hours are collected. A 8,192-row rolling event table records issue, rotation, upload, refusal, expiry and capacity outcomes without capability bytes or full object contents; its oldest rows rotate at the cap. Capacity refusal preserves all unexpired grants and uploaded receipts.

### Make transfer metadata the audit trail

Grant transitions use the workload authority's existing SQLite transaction boundary. Event records contain owner, request id, digest, size, event time and named outcome; they contain no bearer token or source data. This is the durable ledger for delegated object-transfer decisions, not a new placement decision or a second store.

### Roll out additively

The new API is disabled for every existing principal by default. Deploy the LiveStack authority first, then enable `upload_grants` only for the ZZOPS test/train dispatcher. ZZOPS can then ask for a grant and return the opaque upload details to its source-publisher machine. Existing caller PUTs, worker-fenced uploads and consumers stay valid. Rollback disables the flag and ZZOPS stops issuing grants; already accepted objects and jobs remain in their current namespace.

## Risks / Trade-offs

- [The upload acknowledgement may be lost after bytes commit] → Persist the owner/digest/size CAS binding before replying; status reconciles from that durable binding without downloading bytes.
- [Concurrent retry rotates a capability during an active upload] → Hold a per-grant process lock from authorization through CAS receipt commit; return an explicit `grant_in_use` result to competing issue/PUT requests.
- [Capability leaks from an application log] → Keep it in an authorization header, never include it in paths, event rows, logs, responses after the mint call, or status output.
- [Abandoned grants consume state] → Enforce unexpired and total row caps, short grant expiries, terminal-row age collection, and a named capacity refusal before admitting another grant.
- [CAS object exists but grant status did not commit before restart] → Reconcile status against the owner/digest/size CAS index and issue the durable receipt without a second object transfer.
