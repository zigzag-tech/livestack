# Live acceptance and rollout recheck — 2026-10-11

Read-only authenticated Workload API snapshot at `2026-10-11T04:42:41Z`,
plus a read-only SSH check of `zz-joe-e2e-1`'s handler-release directory. No
worker, authority, rollout spec, or policy was changed.

## Canary attempt and cleanup guard

- Task-E2E job `2e1f68076649457abc4e2e59cb537d6b` is `succeeded`; attempt
  `f2b1ef86f0954dd894f857361ec19f76` is `ended` on `zz-joe-e2e-1`.
- Handle `06318b61f53c4b3ca5cb7dc620b5702f` is `parked` at generation 18 with
  `writer_attempt=null`. Its current replica reports 2,297,466,880 bytes on
  host `zz-joe`, profile `benchday-task-e2e-v1`.
- The same roster read reports worker 1 `idle`, ready, claim-enabled at
  generation 13, with no active job. The roster had no active jobs on any
  worker at that read. This clears the active-attempt/cleanup guard for this
  named canary only; it is not blanket authorization to drain or restart other
  workers.

## Current worker-unit inputs

Worker 1 reports release content digest
`93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54` and 17
handler-release digests. A fresh SSH listing of its exact
`handler-releases` directory matches this set:

```text
27f65e8015d4685ca82a8931163f6a6d22042160834d5283c87a512707c77ae2
4157006f260993502c8d4447feeb58248c40a9368e8d1df3f97ea727071951bf
449e0d3b41617dd91146cf0b216f7052dafdf22bf533346beb2f37149095b8b3
4e1671d9c12e6c13c6fcb8eda896e0ccf3e776c92d46644c3df15bdb2ebb728d
742328d4da0a9b9424fe90ee2ae1cd7ae6db2f7155b7da506101ab372ea57331
8463db4f1804a9c6d836267c78da4a57ada79e948c31a342173610234179e830
8747026402ce125158d8c5880817555e55ef091bfbcef75b3c11ee5103bb5d2b
9be5369e10569de3f8a7992a81d636d88dc8f0205fdbe11986a6f752a65db73b
9e2284fc53b217881b37cb35135d8421e6e442e4ef1ada45c108f17061587f36
b9c90168921b75c3f3976679c5dcebede1b681aadb07b381ab481885c3fb2079
c83e20a9086fe99824efc32aa140d7690796f8728be369fb6f892c594897c810
d027f0ff44afce825ea7cf0d16655489a0779e24ff00885de9b2630a22afcaeb
dfd7eb5f2d91f324d4c89632791cf28397a1be810d3b20880b8fe87257fa7390
dfe898eebfc3e3c9028c22c1e46e45b2d15d36711269f8ff2f0fc5ad4e2975aa
f50543375bddf46f439d70a0ccb64c7309493354cbfe047a574fd4cb795d578b
f8766965915c6b1b676376c1894f86b27cdf1482c01e6690d847d547debe6979
fc5f8f930a045e65d65340bd89169dcc461bc152cc69dc18f9209e257b058362
```

The registered rollout unit is still stale: `unit-f38a7baa`, release
`livestack-be1d8a42` (content SHA-256
`6593d104da0263001da63df76a4f7338818053f02e8026de3ccb8b744645d20b`), built
from `be1d8a42` / `be1d8a424684`, with 20 handler digests, capture size 0,
capture cap 2,147,483,648 bytes, `min_authority=c295e76a`, and no verifier.
Worker 1 reports `unit_state=behind`. The current controller remains
`mode=observe`, spec generation 2, report generation 6416, `applied=[]`;
workers 2–5 are `unknown`. Its proposed worker-1 stage/drain/activate actions
were not applied. Automatic selection remains disabled.

The replacement manifest remains unbuilt. The unit schema accepts
`min_authority` as a string but does not define how it is compared with the
active authority; do not infer a new intended minimum from the old value. The
ARM handler is also still pending owner review and implementation.

## Release-plan identifier discrepancy

The owner reported that daemon 3450 plan
`78d0358d-8ad1-4395-96e6-dbd374144caa`, graph
`graph-target-v1-e9a7050f7c833d9de10a0211`, had a Windows3 job
`2a188eab8e214b66b07b8c668e09b0aa` still running. A fresh Workload API `get`
for that same job ID instead returns `succeeded`, handler
`benchday.release.daemon.linux.v1`, attempt
`a69157a805824837920c803973c2dec4` ended on `zz-joe-release`; the returned
spec contains no plan/graph identifier. Therefore this lookup cannot certify
the external graph node's state. The referenced shell waiter 31405 is not
available in this shell, and no local process matching the plan/graph IDs was
found. Do not restart or duplicate work; reconcile with the graph-owned status
before declaring the 3450 plan terminal.

ZZOPS status at the recheck showed no trains or cargos and an open deploy
fence. Its global `gate` field was deliberately not used as a verdict for this
release plan. Full/coalesced E2E and publishing were not run.
