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
configured Windows backing filesystems and their free-space reserve. The
authority/worker services and Benchday handlers are not deployed yet.
