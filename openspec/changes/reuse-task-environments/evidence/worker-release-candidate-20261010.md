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
