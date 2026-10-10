# Native stream producers (openspec services-own-their-streams)

Harmony publishes its own facts and serves its own intents through the Benchday daemon's local ingress.
Everything is OFF unless `HARMONY_STREAMS=1` (optional `HARMONY_STREAMS_SOCKET`, `HARMONY_STREAMS_DIR`,
`HARMONY_UNITS_GLOBS`). Code: `livestack_node/streams/` (pure, stdlib only), wiring in
`livestack_node/hostd_streams.py`, called from `hostd.main()` and `workloads/service.py`.
The Benchday SDK is vendored unchanged in `streams/benchday_streams/` (header names the benchday commit).

| Contract | Producer | Module |
|---|---|---|
| `harmony.host/1` (state, one-per-host) | every hostd | `host_facts.py` |
| `harmony.fleet/1` (state, one-per-realm) | fleet broker (`LIVESTACK_DISPATCH=observe`) | `fleet_facts.py` |
| `harmony.jobs/1` (state, per job) | workload authority process | `fleet_facts.job_body` |
| `harmony.workload/1` (intent, one-per-realm) | workload authority process | `workload_authority.py` |
| `harmony.request/1` (intent, one-per-host) | every hostd | `request_authority.py` |

## Storage bounds (rule 10)

| State | Bound | Enforcer |
|---|---|---|
| `host_facts.json` (last digest, `changed_ms`) | 1 KiB, one record, rewritten in place | `HostFacts._save` |
| workload intent ledger `streams/intents.sqlite` | 1024 live rows, 4 MiB, terminal rows deleted after 7 days, unresolved rows ended `expired` after 30 days | `IntentLedger.put` / `prune` / `overdue` |
| request intent ledger `requests.sqlite` | 256 live rows, 1 MiB, same windows | same class, `request_ledger` |
| published job subjects tracked | 256 (oldest-published forgotten) | `StreamsRuntime._jobs_tick` |

The windows are the catalog's `durable` block for the contract, not env knobs.

## Workload references and authority checks

`harmony.workload/1` resolves a `spec` reference from the WorkloadStore owner's CAS. It accepts only
`application/json`, reads at most 1 MiB, checks the declared byte count and SHA-256 digest, then passes the
decoded object through the same submission validation and handler allowlist as the HTTP API. The CAS object,
the workload `input_digest`, and each `input_objects` entry must all belong to the authenticated writer's
WorkloadStore principal. `body.owner`, when present, must equal that principal; it cannot select another owner.
Cancellation also checks the original requester.

On success, `harmony.workload/1` carries one `output` reference to the canonical JSON result manifest at
`/v1/workloads/jobs/<job-id>/result` (absolute when `public_base_url` is configured). The endpoint uses the
same authenticated owner check as the job endpoint. The bounded manifest includes the accepted spec, job
identity, current attempt identity and compiler grant, plus the handler result with artifact names, digests
and sizes. Artifact bytes remain in the existing content store. The reference digest covers this exact
manifest, so clients can fetch it once after `done` and verify artifacts without polling job status.

The `harmony.jobs/1` reader obtains its capped recent-job set with one SQLite checkout and one query. A cold
replay spaces publications at 40 frames per second, below the daemon ingress limit.

## Where the request language's vocabulary comes from

`requirement_inexpressible` fires when a clause names an attribute no unit in scope declares (the open
vocabulary is the union of declared unit attributes) or a value shape the language cannot evaluate; it
carries `clause` and `detail`. `unsatisfiable` means every clause evaluated and no unit matched.

## Lane driver

`python3 -m <dir>.lane --help` (copy `livestack_node/streams/` anywhere; stdlib only).
