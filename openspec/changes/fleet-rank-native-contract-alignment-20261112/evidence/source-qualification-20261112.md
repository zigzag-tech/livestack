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

Worker-tested source SHA-256 (verified against local candidate before commit):
- `node-py/livestack_node/streams/fleet_rank_stream.py`: `a24b48b33442d35487b49d3e498780ade23518192add6326d09fb1ab302a59e6`
- `node-py/livestack_node/streams/runtime.py`: `f47d628f1f8dc93e2b4996f4a97a90285459b82c063b2c386cf4de3ad082b862`
- `node-py/tests/test_streams_producers.py`: `1f23d8812207a7ba7846b326a0a3addb340531120170a7fca71d0f8d76c3e02f`
- `node-py/tests/test_fleet_rank_stream.py`: `e4cfc2c3b632919930ea41c316be110364d0d2e37e8efd3b43cccf21f42c7adf`

Canonical schema SHA-256: `777ccf75bfbcf987ee3cafd677e01163a0cf27a897e72743d6db3ad6a16dc840`.
Real-ranker body vectors SHA-256: `e966fe93503229581809eb6e77345a6a078f8677733fb699343a4e9af5f48d46`.
