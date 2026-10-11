## Context

See proposal.md - Why. The current authority has `caller`, `worker`, `admin`, and `rollout` principals. Its `/job-changes` route is admin-only and allows an optional owner filter. The daemon source must use a colocated HTTP API and must never receive an admin credential or read the authority database.

## Goals / Non-Goals

**Goals:**
- Bind source reads to one fixed effective owner through the authenticated principal.
- Make the source credential read-only and prevent use on caller or admin routes.
- Preserve bounded row/byte responses and tie-safe pagination.

**Non-Goals:**
- No token minting, live authority reconfiguration, second listener, network exposure, or Benchday daemon changes in this repository.
- No changes to the existing admin route or job mutation permissions.

## Decisions

- Add a `job_reader` role configured with a unique strong token and principal id equal to the owner visible to this source. It has no handlers, delegation prefix, upload grants, worker identity, or other optional privilege. The role is admitted only on the new job-change route; it is excluded from caller/admin dispatch. Reusing a `caller` token was rejected because it also grants job submission and mutation. Allowing an admin token was rejected because its authority is broader than one owner.
- Add `GET /v1/workloads/jobs/changes`. The route accepts `updated_since`, optional `after_job_id`, and `limit`; rejects unknown/repeated parameters and any `owner` parameter; and supplies the authenticated principal id as the projection owner. It returns that owner in response metadata so an importing host can compare it with its operator-configured expected owner, including on an empty page. The old admin route remains compatible and admin-only.
- Use the authority's existing job store as the sole source of truth. The cursor is a request/response value ordered by `(updated_ms, job_id)` and is not persisted. The response remains within 256 rows and 30 KiB; the source reports `truncated` as zero or a positive “more pages” marker, avoiding a full-history count. Effective-owner/timestamp ordering is indexed, and latest attempts are joined for a page in one bounded query rather than one query per job.
- Keep the authority's single configured bind unchanged. A deployment that uses the source must configure an appropriate loopback listener; this code does not guess a port or expose a new interface.

## Risks / Trade-offs

- [A misconfigured `job_reader` principal could name an unintended owner] → The owner is the principal id, delegation is forbidden for this role, the route has no owner argument, and response metadata identifies the binding.
- [A token might be used on another route] → The new role is explicitly denied by all existing caller/admin/worker route families; tests cover the refusal.
- [The live authority may not be reachable on loopback] → Keep bind configuration outside this change and document the deployment precondition; no network bind is broadened by code.
- [Timestamp ties may span pages] → The continuation cursor uses `(updated_ms, job_id)` and regular polling replays the boundary for consumer deduplication.

## Migration Plan

Add a dedicated `job_reader` principal to the authority configuration with a fresh strong token and the exact effective owner id. Restart/reload the authority using its existing principal mechanism, then provision that token only to the colocated source host. Rollback removes the new route and principal; it does not alter job data or the existing admin endpoint.
