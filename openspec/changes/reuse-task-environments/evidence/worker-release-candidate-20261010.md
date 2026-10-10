# Current worker release candidate — 2026-10-10

Built the worker package from current `origin/main`, commit
`e498c8284f77848f1a0c97c6228e585b5266d6e0`, with
`node-py/scripts/build-worker-release.py`. The candidate is
`/tmp/livestack-worker-reuse-task-environments-e498c828`; its content hash is
`8917b440ac5c11055a46f396ed75d290df5e12de2c508faa5bb5ab83534cbba3` across
258 files.

Read-only verification over SSH compared it with the deployed worker package
`~/.local/share/livestack-workload-releases/livestack-377bb4e4` on `zz-joe`.
The deployed package hashes to
`7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2` across
246 files. The current candidate has 12 files missing from the deployed copy
and 6 changed files; there are no deployed-only files. Each deployed version
of a changed file matches a commit on current `origin/main`; no hand-edited
file was found.

This is a local candidate and comparison only. No worker config, service unit,
or process was changed. The separate authority package still needs a fresh
candidate and the required idle, fenced rollout window before deployment.

## Follow-up: retained task-E2E source fix

Livestack commit `185edbf7` added `PYTHONDONTWRITEBYTECODE=1` to the
per-attempt environment for `task_e2e` jobs. Its release was built at
`/tmp/livestack-185edbf7` and staged at
`~/.local/share/livestack-workload-releases/livestack-185edbf7` on `zz-joe`.
The staged release and local build are IDENTICAL: 258 files, content hash
`2f77218e7eb169e971eea4ea623e32601c2a3575012ac005f29528128ccfb564`.

Worker `zz-joe-e2e-1` was drained at claim generation 9. Its active Rust check
finished normally. The service was then restarted with the new release and
verified active with `PYTHONPATH` pointing at `livestack-185edbf7`; worker 1
registered and became ready at generation 10, then was enabled at generation
11. It completed the selected task-E2E scope check and returned to idle. The
independent full/coalesced run on `zz-joe-e2e-2` remained untouched. No authority
package or unrelated worker was changed.
