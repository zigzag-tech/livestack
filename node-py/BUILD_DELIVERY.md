# Development build placement and delivery

Clients negotiate `capabilities.build_delivery.version == 1` before sending
`resource_profile` and `delivery` (consumer_host, expected_output_bytes, up to
16 measured routes). Old requests retain their behavior; unsupported authorities
fail by name. Hard compilation policy, selectors, host holds, current headroom
and capacity apply before preferences. A worker's optional `execution_limit`
also caps full execution need, independently of any smaller admit minimum.

Resource observations are keyed by handler/profile, never mixed between build
modes. Build durations additionally distinguish physical hosts: 20 samples per
series, globally4096 rows and30days, enforced by record_build. Among feasible
workers the scheduler ranks build duration plus delivery cost. Unknown duration
uses a conservative profile observation or request estimate. Route observations
expire after300seconds. Unknown routes use a named conservative build-time
budget. Same-host transfer is zero only for a worker advertising local handoff.
These estimates do not claim measured preparation-cache hits or network rates.

Worker `local_artifacts` configuration requires an absolute private root, host
and store identifier; optional bounds max_bytes/max_handles/ttl_seconds default
to2GiB/64/3600, with hard ceilings8GiB/128/86400. Reports sweep expired handles
and unreferenced objects while idle. A file lock serializes writes and verified
copies. Handler output bounds still apply; files below1MiB use normal CAS.
References bind owner, source, job, attempt/fence, host, digest, size and expiry.
Consumers supply their operator-configured cache root plus exact job_id to
InputTransfer. Worker paths are never accepted from a reference. Copies verify
bytes and recheck the lease before acceptance. Absent/expired local content is
a named failure; local handoff is not a promise of remote durable storage.

Separate principals use POST shares/objects (recipient, digests, ttl_seconds)
and POST jobs/<id>/share (recipient, ttl_seconds). Only the true producer grants;
recipients must be registered callers. These are read-only, not cancellation,
write or re-sharing rights. TTL60..86400, default3600; global4096 job/16384
object grants and128 objects per request are enforced transactionally. Expired
grants are pruned on grant/maintenance. Results include CAS artifacts and bound
local references. Neither party borrows the other's credential.

Private validation:15 new real-authority/SQLite/file checks pass. Broader worker
and resource-history checks:106 pass,6 skip,5 fail. The upstream store_guard
fixture failure and systemd sandbox failure reproduce on unchanged45b9990c;
the optional-mirror HTTP startup failure also reproduces there. They are not
reported as passing evidence for this feature.
