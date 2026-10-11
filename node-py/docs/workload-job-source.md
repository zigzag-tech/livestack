# Owner-bound workload job source

`GET /v1/workloads/jobs/changes` is for a colocated stream producer that needs
to observe one owner's workload state. It uses a dedicated `job_reader`
principal. The route derives the owner from the principal id, returns that id
as response metadata, and refuses an `owner` query parameter. A reader cannot
submit, cancel, upload, read workload objects, or use administration routes.

## Operator provisioning

Add a dedicated principal to the operator-managed workload authority
configuration. Set `id` to the exact effective owner value used by the jobs the
producer must observe. Give it a fresh, unique, high-entropy bearer token with at
least 32 characters and set only the `job_reader` role:

```json
{"id":"<effective-owner-id>","token":"<unique-secret-from-the-operator-secret-store>","role":"job_reader"}
```

The reader has no `handlers`, `delegate_prefix`, worker identity, upload grant,
or other privilege fields. Provision its token only to the colocated producer
through the existing secret-management process. Never reuse or provide an
admin bearer to the producer. This change does not create or activate a
credential, edit the live principal configuration, or deploy the authority.

Before enabling the producer, verify that the existing authority listener is
reachable on loopback at the configured source address and port. Listener
binding remains operator-owned; this feature does not add a listener or widen a
network bind. If loopback reachability is absent, keep the source disabled until
the operator configures and verifies the listener through the normal authority
runbook.

## Polling and bounds

The reader may request `updated_since`, `limit` (1–256), and, while continuing
one page sequence, `after_job_id`. Unknown and repeated parameters are refused.
A timestamp-only poll includes its watermark boundary so changes sharing a
millisecond remain observable; consumers deduplicate those boundary replays.
For a multi-page poll, resume with both `updated_since` and `after_job_id` from
`next_cursor`. The cursor orders rows by `(updated_ms, job_id)`.

Each response contains at most 256 jobs and 30 KiB. `truncated` is zero when
the page is complete and positive when at least one more page is available.
Continue only when a cursor is present. The client verifies the returned owner,
row ordering, cursor, and response bounds before returning data to its caller.
