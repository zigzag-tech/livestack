# Durable Harmony workloads

Status: implementing; companion integration proposal is Benchday
`openspec/changes/harmony-build-test-capacity`. Approved 2026-09-08.

Harmony owns generic workload admission, resource claims and attempt execution.
Benchday owns E2E selection/verdicts and release stage dependencies/signing policy.
The workload service is separate from inference admission: it never converts an
exception into permission and never invokes GPU load/evict actions.

The service persists jobs, attempts, workers and claims in SQLite WAL. Submission
is principal-scoped and idempotent. Admission takes an immediate transaction,
reuses `fleet_scheduler.schedule`, and persists the attempt before returning it.
All worker kinds on a physical host share the resource budget. A worker reports
both configured capacity and measured headroom; absent dimensions cannot grant.
Workers are authenticated, handlers are installed/allowlisted, and job payloads
cannot supply shell commands. Every attempt carries a fence and input digest.

Restart marks workers unready until reconciliation. Expired claims fence results
but retain capacity until the worker confirms owned-process cleanup. A disconnected
host cannot receive another attempt; another host can retry infrastructure work.
Product failure is terminal. Worker-owned heartbeats outlive caller sessions.

Source objects are SHA-256 addressed. A worker verifies them before extraction
into a private source root and writes only to separate output state. Dependencies,
submodules, modes and dirty source provenance belong in the manifest; development
checkout symlinks and secrets do not. Content and log stores enforce byte limits.

Bounds: 1,000 active jobs; 10,000 retained terminal jobs or 14 days; 3 attempts/job;
128 workers; 32 active claims/worker; 64 KiB job/result records. Cleanup runs during
transactions and periodic service sweeps. Referenced input remains pinned; hard
caps reject new admission if nothing can be reclaimed. Retention exemptions are
honored. Unconfigured destructive retention disables deletion, not admission caps.
Service logs use rotation; SQLite WAL checkpoints constrain journal growth.

Rollout: isolated store/process failure tests, two real supervised workers, remote
E2E, then release stages. Win One is WSL Ubuntu via SSH port 2222 for provisioning;
normal worker traffic is outbound HTTP, with no caller-owned SSH session.

## Execution foundation evidence (2026-09-08)

The Linux/WSL executor uses a deterministic per-attempt systemd user service,
with cgroup CPUQuota, MemoryMax, MemorySwapMax=0 and TasksMax. A singleton worker
journal is fsynced before launch; recovery stops the owned unit and verifies its
cgroup is empty before reporting cleanup. The installed wrapper drains command
output into two rotating files (8 MiB each by default). Execution descriptors
and the single active journal are capped at 64 KiB.

Worker-owned heartbeats return a relative lease duration. The worker subtracts
the full request round trip and a stop margin using its monotonic clock, avoiding
cross-host wall-clock assumptions. The wrapper checks the local deadline every
100 ms; it exits on expiration and systemd stops descendants. Renewals therefore
do not depend on the submitting terminal, and a dead supervisor cannot renew a
job indefinitely. Expired claims remain reserved until explicit cleanup.

Real local HTTP/SQLite/systemd tests cover caller disconnect, continued renewal,
cancellation, stale fencing, restart cleanup, resource limits and bounded output.
The same cgroup CPU/RAM/process, journal-restart, lease-expiry/descendant and log
probes passed on xc-win-1 WSL through ubuntu@100.64.0.1:2222. The render container
was left running. This is foundation evidence, not worker enrollment: Docker
container ownership, filesystem bounds, the persistent worker service and product
adapters are still required before production handlers may be advertised.

Completion results may name up to 128 artifacts as `{name, digest, size}`.
The completion transaction verifies each object is ready, owned by the job's
principal and has the declared size, and rejects duplicate names. Retention
protects output references from every attempt, including infrastructure attempts
preceding a successful retry. Job retention and its explicit exemptions control
when these references can be released. The blob sweep materializes this bounded
reference set once per sweep. Real SQLite/blob tests verify both preservation
and eventual removal, and reject missing, foreign and mismatched artifacts.

The native worker now polls independently, measures RAM/CPU/disk headroom,
downloads and verifies the assigned source, launches an installed handler,
uploads bounded logs and declared artifacts, and acknowledges completion only
after cgroup cleanup. Its durable journal replays an ambiguous completion or
reconciles an interrupted attempt after restart. Observe-only reports capacity
without claiming jobs. Native execution does not yet advertise Docker handlers.

Production workers require a separate bounded filesystem. The root-only
`workloads.provision_workspace` command exclusively creates an owned ext4 image
under `/var/lib/livestack-workloads`, installs an enabled systemd mount unit and
verifies the mounted backing file. It refuses changed sizes or unknown existing
images. A real Win One 1 GiB fixture proved idempotency, mismatch refusal and
ENOSPC at its bound, then was removed after verifying loop-device detachment.
Win One now has its persistent 128 GiB worker filesystem mounted at
`/var/lib/livestack-workloads/xc-win-1-wsl/workspace`. WSL reports also account for
configured Windows backing filesystems and their free-space reserve.

## Enrollment deployment (2026-09-08)

Livestack main is shipped through `2da5f8fb4ccc155cfc559351044bb3a19233d6b2`.
The authority runs as the enabled user service `livestack-workload-authority`
on xc-tower-ubuntu, bound only to `100.64.0.18:8810`, with a 32 GiB object cap.
Win One runs the enabled user service `livestack-workload-worker`; user lingering
and its root mount unit are enabled. Both services load the same immutable
release directory under `~/.local/share/livestack-workload-releases/<commit>`.
Credentials are private files under `~/.config/livestack-workloads`, outside git.

The worker first reported fresh CPU/RAM/disk headroom in observe-only mode, then
enabled only `harmony.probe.v1`. Job `adff3bf47b554490855d78a9b9935d68` was submitted
from the tower and completed by `xc-win-1-wsl` after the submitting process exited.
Its returned artifact proved the exact source manifest, attempt identity,
0.1 CPU quota and 128 MiB memory cap. The real Win One workload/scheduler suite
passed 68 checks; the subsequent configurable-quota authority CLI test passed
locally. E2E/Docker and release handlers are not advertised; product integration
and full Benchday gate/publish remain required.

## Rootless Docker execution backend

Installed handlers may select `backend: rootless-docker`. The supervisor starts
RootlessKit in a delegated systemd unit, with its control processes in a
`supervisor` subgroup and Docker's explicit `--cgroup-parent` pointing to the
unit itself. This explicit parent is essential: a real Win One probe with only
rootless mode and `native.cgroupdriver=cgroupfs` put containers in a sibling
cgroup, outside the job's limit. With delegation and the explicit parent, a real
container inherited the 2 GiB/one-CPU parent cap and disappeared after unit stop.

The daemon and registered handler share a private user/mount/network namespace.
Images and writable layers live in the job's `docker-data` on the bounded worker
filesystem, bind-mounted at a short namespace-local path to avoid Unix-socket
path limits. Client configuration is private too; inherited Docker contexts or
Buildx endpoints cannot select a developer's builder. Fixed handler commands
remain responsible for requesting any exclusive host ports they publish; the
rootless port driver does not make published host ports collision-free.

Docker logs flow through the existing bounded execution writer. Container logs
rotate at 4 MiB x 2 per container and all Docker storage remains under the hard
workspace byte cap. Short RootlessKit control state lives under
`/run/user/<uid>/hw-<unit-hash>`: one active directory per worker attempt, removed
only after its cgroup is empty and an owner marker matches. Interrupted attempts
retain their cleanup claim. Subordinate-UID image files are deleted through a
bounded-time rootless cleanup command before completion/cleanup acknowledgement.
The production controller's service cap also covers that cleanup subprocess.
Exit code 75 is reserved for Docker preparation infrastructure failures.

Win One now has `uidmap`, `rootlesskit`, `slirp4netns` and `docker-buildx`
installed. Package installation did not replace or restart its host Docker daemon;
`unchain-render-worker` retained start time `2026-09-08T01:33:09.570642946Z`.
Live enrollment still advertises only the probe; the new backend is not yet
connected to Benchday E2E, release handlers, persistent image caches or the train.

Validation on Win One: the complete workload/scheduler suite passed 75 checks
in 66.57 s, including real Docker container and BuildKit process membership,
CPU/RAM inheritance, lease-triggered teardown, subordinate-UID layer cleanup,
HTTP worker source/artifact delivery and product-failure classification. The
worker's minimal environment now receives a private `XDG_RUNTIME_DIR` and the
system-administration executable paths Docker needs. Infrastructure failures
retain bounded log artifacts; uploads refused by a cancellation fence proceed
to reconciliation instead of escaping the worker as an uncaught HTTP error.
The host Docker socket's copy-up alias is also removed from the private namespace.

## Live dependency workload (2026-09-08)

The authority and Win One worker now load immutable release
`cbf8c1f744e580016f02e4925fd90af522363560`, superseding the probe-only
rollout described above. The generic CLI captures, uploads, submits, reads
status and downloads artifacts using the existing authenticated APIs. It
requires no build tools on the submitting machine.

Benchday owns the installed `benchday.e2e.dependencies.v1` handler. Job
`8f61381385a24e94922777cd399e6cfc` transferred a 488,755,200-byte exact-source
archive and completed four cold locked npm installs on Win One in 58.80 s.
The submitting process had exited; the worker completed and returned the
preparation artifact through the authority. Source SHA-256:
`2961eba795221c4705662d2ad42777392a5716100f870ce8a20552a54f6c8f37`.
Preparation artifact SHA-256:
`d380c11f4c1fec88070c6478ed9ab72d7c4badbda3a06295d8d5f4928ca0d976`.
This proves a real cross-machine dependency job, not a full E2E or release.

Benchday handler release `5fe6524dec8f2aeab10d62e02507c740dd69ecd8` is now
installed with complete captured-file verification around dependency installs.
Job `dd4bbd696ce4406eaf1b1172d7dc35ef` validates that stronger handler against
the same input and succeeded with every captured file preserved. Its preparation
artifact SHA-256 is
`ce42b8b0349e631c495435f905430f501b857187cf1356da4bfc83ffcdbcd7c6`. The existing render
container remained running with start time `2026-09-08T01:33:09.570642946Z`.
Persistent caches, train/release routing, shared co-resident accounting and
the full Benchday test/publish gates remain required.

## Process-budget evidence from the first full Benchday job

Job `c25019d6a8b24599bd537c0d51be7457` built its cold private image and
started real E2E assertions. It ended when runc could not spawn another process;
the harness reported assertion-phase failure plus unclean teardown. The fixed
512-task default was a likely constraint, but the old cgroup was already gone
before its pids counter was read. Recorded memory counters showed zero OOM
kills, about 2 GiB anonymous memory and 6 GiB file cache at the 8 GiB cap.
The worker stopped its cgroup and cleaned the workspace; the host render
container retained its original start time.

Installed handlers now specify `max_tasks` (default 512, integer 1–8192).
The bounded wrapper records fixed-size cgroup CPU usage, peak memory, peak
process count where supported, OOM kills and pids-limit events before exiting.
A nonzero exit with a resource-limit event is infrastructure, not product
failure. These counters use the existing bounded execution receipt/job store.
`infrastructure_outputs` explicitly retains handler diagnostics for such failures.
A real 12-task test proves refusal, kernel evidence, cleanup, retained artifact
and the existing bounded infrastructure retry path. Benchday separately treats
unclean harness teardown as infrastructure, irrespective of the assertion phase.

Deployment update: worker runtime `7564ee8cd08c9221466e0dd1a5550efa288cb1b7`
is enabled on Win One after 22 real supervisor/worker/Docker checks passed in
66.44 s. The authority still runs `cbf8c1f744e580016f02e4925fd90af522363560`;
its protocol and store did not change. Full handler release
`23f0f8fccfe881737f2384e7008683a898d9fc37` uses `max_tasks: 2048` and retains
`preparation.json` and `e2e-result.json` even on infrastructure failure.
Second full job `b1e02a45dd394cfb99de15fe31691145`, attempt
`49738f3cea184bc08dc65e39f8ba1008`, is running against the same accepted source
bundle. This is a validation attempt, not a reported passing gate. Do not
restart or replace it merely because an observation request times out.

## Worker source cache

`input_cache_bytes` enables an owner-scoped, SHA-256-verified archive cache on
the worker's dedicated workspace filesystem. It is disabled unless configured.
`input_cache_entries` is bounded to 1–32; metadata is bounded to 64 KiB and
atomically fsynced. Misses reserve the maximum accepted transfer before writing
staging bytes, so downloads cannot temporarily exceed the cache byte budget.
Every hit verifies the complete archive digest before extraction.

Idle worker steps prune expired entries after restart reconciliation; admission
may evict least-recently-used unretained entries at capacity. `retain` is sticky
per owner/digest. A null retention window disables eviction/expiry and a full
cache refuses rather than deleting content. No pruning runs while an attempt
uses its input. Crash leftovers are owned, uncommitted staging files and are
reconciled separately. The dedicated filesystem remains the hard overall bound;
cache use is automatically reflected in measured disk headroom.

Real HTTP/systemd checks prove a second accepted job runs after the authority's
source file is removed, including across supervisor restart; ordinary cache
entries evict at pressure, retained entries do not, and a disabled deletion
window refuses capacity. Corrupt cached bytes are refused before execution.
The cache is not yet enabled on the production worker because its current full
E2E validation attempt is still running. Image/build caches remain separate work.
