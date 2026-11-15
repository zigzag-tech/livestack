# Current-main producer integration candidate

Fresh worktree starts from origin/main
`b46ec73d85e688a1526408e40f1b9fbb131c61f8`. The existing clean native producer
candidate `0d6c27a707ea4a8afa6e4cf4688349429c0b4cb4` merged without conflicts;
the existing candidate and its pinned Benchday captures remain unchanged.
Pre-evidence staged tree `d8e862274a776ce4e46cce4e3d46cd67bb2b0dec`.

All 65 glob-discovered stream controls pass on the private node-f staging
snapshot `/home/ubuntu/agent-builds/livestack-rank-current-main-20261114/node-py`:

`python3 -B -m unittest discover -s tests -p 'test_*stream*.py' -v`

Result **65 passed, 0 failed**, 22.935 seconds. Log
`/tmp/livestack-rank-current-main-20261114-streams.log`, SHA256
`0217c031312ea5c020cb62235e250ffc39410f04e260e8b9d601c1cc97633cdd`.
The run includes real Unix SDK transport, ranking-only startup, actual ranking
projection, absence/failure/shadow controls, and production factory/store
workload authority admission ordering. Existing SQLite ResourceWarnings and
asyncio slow-task diagnostics are present; no assertion was skipped or failed.
Strict change validation and staged whitespace checks pass.

This candidate brings the producer onto current main for continued review. It
does not establish real Benchday daemon authority, Hub reconnect/fresh-cut
behavior, production activation or legacy poller/frame retirement. Those still
require the exact producer/consumer/Rust/Task-E2E receipts. The active Benchday
Rust request captures `3cfc68785175b0071d1bb49c36e87897c07004df`; existing
realm captures must be preserved, not duplicated or silently treated as proof
of this newly merged source. The source change remains draft and unarchived.
