# Native producer source qualification — 2026-11-12

Fresh baseline: Livestack origin/main 1272b7e6. The implementation aligns to
Benchday catalog commit b480f0a7d2 and consumer commit 75e1630417, whose typed
branch verdicts remain separate. The source uses harmony.fleet.rank/1 and pair
subjects, 32 configured pairs, one vantage per pair, strict typed preferences,
content-sensitive IDs, and bodyless no-target absence. It changes no production
activation, credentials, listener or daemon service.

Regular checks on the private zz-joe source snapshot:
- 33/33 Python stream-producer/projection tests passed.
- 11/11 directly scoped fleet projection/SDK socket controls passed.
- Two bodies produced by the real LLM and ASR ranker passed the exact canonical
  JSON Schema through Ajv (including actual ASR preference/receipt vocabulary).
- OpenSpec change validates; git diff --check passes.

The first broader module run failed because a new workload could be submitted
while initial on_up reannouncement was still finishing, replaying admitted.
One unmodified upstream run passed, so this was not a reproduced baseline
failure. The test setup now waits for the initial reannouncement boundary before
submitting. Its exact five-rung assertion remains unchanged; the final full
module passed. Existing SQLite ResourceWarnings remain in that test module.

The SDK tests use a private AF_UNIX protocol fixture. They do NOT prove real
Hub/daemon authority registration, production activation or final-tree Task-E2E.
Those remain open in tasks 2.1, 2.3 and 2.4. This change is not ready to archive.

Shadow-acknowledgement follow-up: the same guard now checks present, absent
and failed publications. A shadow acknowledgement raises the named
fleet_rank_authority_refused:shadowed outcome for every case. This closes the
absence/failure branches that previously ignored that acknowledgement.
The exact updated worker source passed 36/36 module tests, including all 14
fleet-focused cases and three SDK socket shadow controls. No real authority
admission or production activation is inferred from the protocol fixture.
The projection module and its previously validated real-ranker bodies did not
change; the two schema-vector results remain applicable to that same module.

Worker-tested source SHA-256 (updated hashes verified locally before commit):
- `node-py/livestack_node/streams/fleet_rank_stream.py`: `a24b48b33442d35487b49d3e498780ade23518192add6326d09fb1ab302a59e6`
- `node-py/livestack_node/streams/runtime.py`: `02b59abbda990c5de3a5ddc4f5a0c18e4b329a6956d0992346ecb8510f257c00`
- `node-py/tests/test_streams_producers.py`: `1f23d8812207a7ba7846b326a0a3addb340531120170a7fca71d0f8d76c3e02f`
- `node-py/tests/test_fleet_rank_stream.py`: `cd9a36251ebc674bd54af01dad601bcfc9d08c99cbd77bbbefe537d02ee7377d`

Canonical schema SHA-256: `777ccf75bfbcf987ee3cafd677e01163a0cf27a897e72743d6db3ad6a16dc840`.
Real-ranker body vectors SHA-256: `e966fe93503229581809eb6e77345a6a078f8677733fb699343a4e9af5f48d46`.
# State-registration SDK alignment (2026-11-13)

The first native fixture inspection found that this source's older vendored SDK
sent `register` and interpreted a reply without explicit `mode: active` as
shadow. The current daemon treats register as a serve alias and returns
`serving` plus epoch for active state registration, without a mode field.
Re-vendored the unchanged Python client from Benchday commit
`99f1f1a9743780effd512102ab8a19d421ed6f5d`. The AF_UNIX protocol mirror now
answers both spellings as the daemon does; explicit shadow returns no active
epoch. Added a control asserting that serving without mode is active.

On the exact updated private source snapshot on zz-joe:

- `unittest discover -s tests -p 'test_fleet_rank_stream.py'`: **10 passed**.
- `unittest discover -s tests -p 'test_identity_fact_streams.py'`: **22 passed**.
- `unittest discover -s tests -p 'test*streams*.py'`: **52 passed, 1 failed**.
  The failure is `WorkloadTests.test_ladder_to_done_and_every_rung_is_published_in_order`:
  observed submitted, admitted, admitted, placed, running, done. The exact
  five-rung assertion is preserved. This run predates the protocol mirror's
  final removal of epoch from its explicit-shadow response; it is not a final
  all-streams receipt and must not be represented as one. The earlier evidence
  above already records this duplicate-admitted failure shape.

The two passing discovery runs above used the final mirror and source, with a
checksum-only rsync comparison confirming no worker/source differences. The
OpenSpec change validates. These remain SDK socket/projection controls, not
real daemon/Hub authority evidence. Task 2.1 and integration/archive obligations
remain open; no production activation or source landing is inferred.

## Workload lifecycle race fixed (2026-11-13)

The duplicate-admitted failure is now reproduced deterministically using the
production authority factory, real WorkloadStore/CAS and framed SDK. Holding
the initial submitted publication while invoking the pump produced
`admitted, submitted, admitted` before the fix: the pump mistook an in-flight
submit's durable row for crash recovery. This is stronger evidence than the
earlier startup-timing hypothesis; waiting for initial reannouncement alone
does not fix that race.

The authority now serializes write, pump and reannouncement mutations with
one asyncio lock per authority (no growing per-intent lock map). The exact
owner, reference, admission, cancellation and lifecycle assertions remain.
The new real-store regression fails before this fix and passes afterward,
requiring submitted/admitted seq 1/2 and exactly one real job with no attempt.

Final private zz-joe source: `unittest discover -s tests -p 'test*stream*.py'`
passed **64/64**, including the previously failing five-rung assertion, the new
concurrent pump regression, fleet/identity and real workload-store controls.
OpenSpec validates and diff checking passes. Existing SQLite ResourceWarnings
remain visible. These results supersede the earlier regular-test failure,
but do not supply real daemon/Hub authority or final-tree Task-E2E evidence.
