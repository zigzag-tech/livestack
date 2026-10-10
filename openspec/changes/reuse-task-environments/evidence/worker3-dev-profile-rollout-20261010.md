# Worker 3 development-profile rollout — 2026-10-10

Worker `zz-joe-e2e-3` was idle before the update. Its service now uses the
Livestack release built from current `origin/main`
`1cfdc69b0ca545e11d871bc8d0a6f603dfbd86f6`, content hash
`8917b440ac5c11055a46f396ed75d290df5e12de2c508faa5bb5ab83534cbba3` (258
files). The installed release verified identical to the staged candidate.
Only worker 3 was restarted; the authority and other workers were untouched.

The mode-0600 config candidate was derived from the live worker 3 config and
adds only the Flutter and Rust development profiles. Its SHA-256 is
`d83f0270c48386901db6f52b72f0ea9a782f925af42efbe2afed8a3a5635bbd6`. The
candidate passed the current `TaskEnvironmentStore._profiles` validation for
the two installed compilation handlers. The task-E2E profile was not added.
Original config and release-pointer backups are in
`/home/ubuntu/.local/state/livestack-workloads/backups/reuse-task-environments-20261010/`.

The worker was drained with owner `codex:reuse-task-environments`, expiry
1,800 seconds, and generation CAS 3; it restarted while idle and was enabled
with CAS generation 4. The final claim generation is 5, enabled, with no
`needs_operator`, activation failures, or task-environment warnings. The
worker reports handler registry generation 62.

Immediately after it became eligible, ZZOPS assigned an existing full-suite
admission to the worker. This was not submitted by the task and was not
interrupted. Flutter environment job
`3520e1a97e4b45789cea2f05890bc3cd` is queued; its scheduler reason lists
worker 3 as `worker_busy` rather than `environment_profile_not_installed`,
confirming that the new profiles reached placement.

Worker 4 was also idle, so it received the same verified release and its
mode-0600 candidate config adding only the Flutter/Rust development profiles.
Candidate config SHA-256 is
`a7a4756cb9b80793b0de802d333f78d753cf731bec22425a9ee6202ef3005583`; backup
files are under
`/home/ubuntu/.local/state/livestack-workloads/backups/reuse-task-environments-20261010/worker-4/`.
Worker 4 was drained/re-enabled with generation CAS 3→4→5 and restarted while
idle. It reported handler generation 62 with no activation failures or
task-environment warnings; it did not receive a task-E2E profile.

The Flutter environment job then started on worker 4 using the same handle as
previous runs on workers 1 and 2. It is running at generation 24 with
`last_outcome: reused`; its terminal receipt is pending. The exact Benchday
wrapper and assertion result are recorded in the Benchday task's corresponding
`evidence/worker3-dev-profile-rollout-20261010.md`.

Terminal follow-up: the job succeeded on worker 4. Attempt
`51ade18444264a669bbe7719e9965e1b` returned generation 24,
`reuse_outcome=reused`, `source_updated_incrementally`, and `state=parked`.
It waited 178.866 s in queue and executed in 59.277 s. Source digest is
`993f60f7e6d4bf6ec25f1928b303beb4c0834be55826043f870e34f1a4ff8239`; source
manifest digest is
`78e5520aa48eaa132e568c933ed48cecba006619a18028a287047dec5b224a2b`.
The reused `flutter-native` cache identity is
`fad9fd21ed6d9ff6d1b18fa242a2994bf4b792c95fde32442397893ca0249`.
The selected widget test passed and cleanup parked the environment after
execution. Details are in the paired Benchday evidence.
