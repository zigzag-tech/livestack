# Recognized Joe broker ownership and qualification admission

This is a preparation record. The new owner source is not installed. It does
not authorize a broker restart, provider inference, or claim the old process
has drained.

## Actual installed boundary

Read-only inspection on 2026-10-12 found `livestack-hostd.service` PID 966,
InvocationID `98698bd4961c4041b9b95e06d8e70d90`, serving port 8799 from
`sensenova-base-16575876/node-py`. Its hostd bytes hash to
`07014a11039fbfe545f4204c9d6217bbb8d17aa02f7a0b24ad996fe2b8f455ac`.
Its API has no qualification admission or owner fence route. Its active
provisioning-operation list was empty; that does not prove inference or warm
futures are empty. The default durable operation store is
`~/.cache/livestack/fleet-operations.sqlite3`.

The live ZZOPS service reads `/etc/zzops/service.json`; only Benchday and Askafox
are registered there. Neither their credentials nor their targets authorize
this broker's deployment. The PolyTTS `provider.qualify` grant is also separate.

The qualified cold PolyTTS process remains held, unchanged, on port 18100.
Its exact unit kind is `voxcpm`, device `zz-joe/2049e39f`. The qualification
controller verifies its node/source/unit/Unix-peer binding independently;
it must not announce or rank that private node as a normal model peer.

## Source accounting implemented

`BrokerOwnerGate` is an explicit optional argument to `build_app` and
`configured_broker_app`. Absent it, the original branch remains unchanged.
An owner-enabled application requires a private durable journal and the same
broker object. Untracked provision pools refuse this mode.

The gate holds effectful HTTP ingress, tracks actual synchronous handlers and
accepted underlying executor futures, and gates proactive planning and
allocator writes. Existing lease heartbeat/release routes retain their
original authentication and remain available while ingress is held.
Cancellation of an HTTP caller does not settle its still-running worker.
The reconciliation thread has an owner stop event and join barrier.

Dispatch intents are committed to the existing SQLite writer conventions
before remote calls. The journal retains at most 64 records without pruning
unknown work. Exact source/incarnation/nonce/input identity and settlement CAS
are required. A remote success reply is not an accelerator barrier and remains
unknown. Reopening unknown work never reissues it. Placement TTL expiry cannot
clear the durable uncertainty.

The current dispatcher freezes peer object inventory; a changed inventory
refuses planning rather than bypassing remote accounting. A future installed
owner needs either a complete preserved normal inventory or an authenticated,
physically held inventory transition. There is no implemented remote physical
reconciliation operation yet. Those are operational gates, not passing claims.

## Exact supported onboarding interfaces

The installed CLI supports the following read-only preparation command:

```sh
zzops app init harmony-broker \
  --compile-gate 'env PYTHONPATH=node-py python3 -B -m unittest discover -s node-py/tests -p test_broker_owner.py' \
  --dir /home/ubuntu/livestack --dry-run
```

The compile field is an argv array. A leading bare `PYTHONPATH=...` would be
an invalid executable; it is not evaluated as a shell assignment.

Registration follows the actual `docs/onboarding-an-app.md` and
`docs/adapter-bundles.md` interfaces in ZZOPS:

1. Commit a dedicated app policy and actual broker deployment handler. Build
   its adapter through `zzops adapter bundle` at the exact full revision.
   The descriptor must pin the emitted manifest digest. No policy/handler
   artifact has been qualified yet.
2. Independently register that named handler and dispatcher principal in the
   existing Workload authority. The handler-release CLI supports `stage`,
   `activate`, `rollback`, exact generation CAS and immutable release digests.
   Its operator credential is an actual separately provisioned authority role;
   a generated file cannot supply that permission.
3. Prepare a dedicated `harmony-broker` service registration. Existing
   `DeployConfig` supports an app-owned handler and exact target selector/URL;
   the service authorizes `deploy.submit` and `deploy.view`. No generic new
   action or borrowed app grant is required. The target must identify the
   actual guarded Joe worker; a declared selector is not a worker registration.
4. Prepare independent private operator/client custody and explicit canonical
   app grants using `enroll-machine` / `grant-principal`. Preserve all existing
   registrations, secrets and key prefixes. Do not copy the authority DB secret
   onto Joe or reuse the provider qualification key/config.
5. Validate the exact paired source/config against the installed SDK, then use
   its existing fenced self-deployment contract for a service registration
   change. The current coordinator fence does not itself drain broker work.

These are existing interfaces and prerequisites, not commands to execute with
invented handler names, credentials, source pins or a temporary service config.

## Before a broker effect tuple may be admitted

Retain the exact old unit/environment/source, boot/process incarnation,
operation database plus WAL/SHM state, ledger and snapshot schemas and bytes,
hosted/node leases, membership and all pending/uncertain remote effects.
Determine their actual default paths from the installed source and process
environment. Preserve history rather than replacing it with an empty store.

The old daemon and peer futures need independent evidence. Its synthesis
counter excludes `/model/warm`; a timed-out request can leave an executor
future alive. Therefore the new gate, an empty operation list, current GPU free
memory or a successful Uvicorn HTTP drain cannot certify old work settled.
No supported old ingress hold/physical reconciliation path has been proven.

Keep a byte-bound rollback retaining accepted jobs and leases. Every privileged
effect requires fresh canonical deployment permission and exact unit/source/
config/epoch checks. Unknown acknowledgement is status/reconciliation of the
same intent, never another restart. Controls retain the 20-second transport
bound; long source verification is a separate offline operation.

## First native English model gate remains separate

Only the recognized 8799 broker can protect capacity from its normal callers.
A separate private broker's SQLite claim is invisible to 8799 and is not a
substitute. Legacy TTS 8100 and image 8214 also share GPU 2049 and can accept
direct inference; their possible unowned prepares remain a separate admission
and peak-capacity gate.

The first reviewed native-English request retains its exact Python digest
`9c9745a15e512221633e9ac0a72f3cbc22b25257e9b4f6268e1c78f980534571`.
No model window or input grant is created by this document. After real owner
registration, recognized retained capacity and an active bound node kind lease,
the controlled sample/cancel probe must establish actual CUDA settlement before
accepting audio. Cold/source/CPU fixtures do not establish it.
