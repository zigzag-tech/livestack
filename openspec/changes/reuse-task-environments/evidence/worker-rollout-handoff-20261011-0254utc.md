# Task-environment worker rollout handoff

## Superseding release and worker-state correction

The original 02:54Z release status below is historical. The owner confirms all
five daemon 3448 targets are terminal succeeded; publication failed only on
artifact-read 404, and the namespace fix plus full 47 MB artifact and
foreign-404 control passed. Release 3448 is no longer a build or worker-drain
hold.

Root retry 3450 is plan `78d0358d-8ad1-4395-96e6-dbd374144caa`, graph
`graph-target-v1-e9a7050f7c833d9de10a0211`. The owner later confirmed all five
targets succeeded and publication completed under train
`20261011T033118Z-90012`. This supersedes the Windows3-running note and root
wait session `31405` below; 3450 is no longer a build or worker-drain hold.
Preserve its completed artifacts without restart or duplication. The earlier
non-graph-qualified Workload API response did not establish the graph result.

The later authenticated worker roster at 05:11:53Z supersedes the 04:01Z
idle-worker snapshot: e2e-1 was busy on image job
`7e8ebe2e123f40eb84f2f7a1df314098`, and e2e-2 was running coalesced full E2E
job `8d28eb3940d34c7faefb4549f797dcbf`. The 04:53:42Z parked-handle read
clears the cleanup guard for that exact canary only; any later target-worker
action requires a fresh attempt/cleanup read. See the paired
`live-runtime-readback-20261011-0511utc.md` and
`release-status-correction-20261011.md`.

Original read-only operator snapshot prepared at 2026-10-11T02:54Z. Its release
status is superseded by the owner correction above. No worker, authority,
rollout spec, quota, or selector was changed for that snapshot.

## Candidate package and stale unit

- Prepared package: `/tmp/livestack-taskenv-worker-2109995`, built from
  Livestack commit `2109995bcaa9551ad5bf6e0fd1a61d1b4744bd38`.
- Package SHA-256: `93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54`
  (258 files).
- `build-worker-release.py verify` found it byte-identical to the immutable
  package already on `zz-joe`:
  `/home/ubuntu/.local/share/livestack-workload-releases/livestack-23419ff0`
  (`RELEASE.json` commit `23419ff00ffb1f8bcd5ec829a45b5706a25d6f29`, same
  digest and file count). Worker 1's effective `PYTHONPATH` already points to
  that package. Do not copy or rebuild it as part of this rollout.
- The actual stale deployment unit is `unit-f38a7baa`, registered in the live
  authority on `xc-tower-ubuntu`; it targets the `zz-joe-e2e` set on physical
  host `zz-joe`. Its manifest names `livestack-be1d8a42`, package hash
  `6593d104da0263001da63df76a4f7338818053f02e8026de3ccb8b744645d20b`,
  `built_from` `be1d8a42` / `be1d8a424684`, and `min_authority` `c295e76a`.
  The active authority release is `livestack-c2930611`; do not infer a
  compatible minimum-authority value by comparing these labels. Build a new
  unit only after its live handler inputs and authority minimum are read back.
- At 04:01Z, after worker 1 became idle, its current release path/hash and 17
  handler digests were re-read. The handler list exactly matched the on-disk
  release directories. The manifest remains unbuilt: verifier is null,
  capture-size/cap values are carried from the stale unit and not remeasured,
  and the minimum-authority meaning remains unresolved. Do not reuse the stale
  unit manifest.

## Live state in the original 02:54Z snapshot

Fresh authenticated read-only authority read, approximately
2026-10-11T02:54Z:

- Authority release: `livestack-c2930611` on `xc-tower-ubuntu`.
- Rollout spec generation 2; mode `observe`; report generation 6201; `applied`
  is empty. The reconciler's only `would_do` sequence is for
  `zz-joe-e2e-1`; its state is `behind`. Workers 2–5 are `unknown`. The
  controller has no executor: `enforce` is refused by the current schema.
- Worker 1 (`zz-joe-e2e-1`, host `zz-joe`) reports code release digest
  `93fcb848...`, state `running`, `unit_state=behind`, and effective path
  `/home/ubuntu/.local/share/livestack-workload-releases/livestack-23419ff0/node-py`.
  Its claim is currently enabled at generation 13, owner
  `codex:reuse-task-environments`, with no expiry and `needs_operator=false`.
  That is not the user's new operator claim and must not be reused as one.
- The same roster read listed Rust handlers only on five Linux x86_64 workers
  on `zz-joe`: workers 1–5, all with target `x86_64-unknown-linux-gnu`.
  Workers 1 and 2 were `running` in that snapshot; workers 3–5 were `idle`.
  The authority did not map release 3448's five target builds to these worker
  IDs, so this observation did not verify the graph's target states.
- Current task-environment handler/profile and per-handle quota state for every
  worker is not present in the public roster response; read it from the
  operator-approved worker configuration/authority path before accepting the
  rollout.

## Supported worker fence and command

The documented worker fence is the Livestack claims API: an expiring drain
with an owner and compare-and-swap generation. The worker release runbook does
not define a single `zzops deploy` command for this operation; ZZOPS website
deploy and the authority-release script are different operations. The drain
does not kill an admitted attempt.

Release 3448 is terminal and no longer gates this operation. Before any
target-worker action, re-read the target's claim generation, worker jobs,
cleanup/result handoff, and host-owned compiler processes; the independent
task-E2E attempt/cleanup guard remains mandatory. If worker 1 is still the
selected canary and the claim generation is still 13, the supported drain form is:

```sh
PYTHONPATH=/home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611/node-py \
python3 -m livestack_node.workloads.cli \
  --config "$HOME/.config/livestack-workloads/<accepted-operator-or-rollout-config>.json" \
  drain zz-joe-e2e-1 \
  --ttl 3600 \
  --reason "task-environment worker canary" \
  --owner "<owner label from the newly accepted user-account claim>" \
  --if-generation 13
```

The configuration path and owner label are **not verified here**. Use the
operator's genuine accepted identity and a `rollout` or `admin` principal; do
not use `client.json`, an agent/pane identity, or `--force`. If the generation
has changed, use the fresh returned value instead of 13. A drain can begin
while a job runs, but do not restart until the attempt, cleanup, result
handoff, and any host-owned harness process have all ended. The command's
receipt supplies the actual generation and expiry.

After staging/activation and readback, restore claims with the same accepted
owner and the exact generation returned by the drain:

```sh
PYTHONPATH=/home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611/node-py \
python3 -m livestack_node.workloads.cli \
  --config "$HOME/.config/livestack-workloads/<accepted-operator-or-rollout-config>.json" \
  enable zz-joe-e2e-1 \
  --owner "<same accepted owner label>" \
  --if-generation <generation-returned-by-drain>
```

Do not edit or restart the authority. Before changing a worker drop-in, read
the complete effective `systemctl --user cat` output: worker 1 has several
high-numbered release/task-environment drop-ins, so restoring only
`70-handler-release-registry.conf` could leave a later override active. Keep
the existing release directory; if the worker package truly changes, stage a
new commit-named directory and verify its content hash before switching the
single worker unit.

## Rollback and readback

- Keep the old spec and `unit-f38a7baa` registered. If a new spec is posted,
  rollback it with compare-and-swap using the generation returned by that post;
  restore the previous `unit-f38a7baa` target and `mode=observe`. Never force a
  stale spec write.
- Preserve a byte-exact copy and digest of the target worker's effective
  drop-in/config before editing. The last verified code package path is
  `.../livestack-23419ff0/node-py` with digest `93fcb848...`. If the new worker
  release fails, point only that worker back to the previous path, reload the
  user unit manager, restart only that now-idle worker, then enable it with the
  returned claim generation. Do not restart the authority or other workers.
- Before re-enabling, require worker ready/claim disabled during restart,
  clean startup, expected release and handler digests, expected `unit_state`,
  task-environment profile and host-bounded quota readback, no stale cleanup
  or active attempt, and successful scoped probe. After enable, verify
  `enabled=true`, the new claim generation, fresh heartbeat, unit `current`,
  and a representative task-specific result with resource cleanup. Keep full
  E2E, publishing, and automatic selection outside this operation.
- Production rollback of this worker has **not** been exercised. The recorded
  disposable rollback drill covered the authority script's failure branch
  only, not worker unit rollback.

## Corrected alternate-ABI Rust enrollment and Lima readback

This section supersedes the earlier conclusion that no Lima guest existed. The
operator correction was independently confirmed with the non-default Lima home.

### Verified Linux guest

- Mac host: xc-mac-studio, mesh IP 100.64.0.2.
- Lima home: /Users/ubuntu/.lima-harmony. Instance xc-mac-studio-harmony is
  Running, aarch64, 8 CPUs, and 16 GiB memory.
- Guest account: ubuntu, home /home/ubuntu.guest. Rust is installed at
  /home/ubuntu.guest/.cargo/bin/rustc, version 1.99.0; the installed target is
  aarch64-unknown-linux-gnu.
- Read at 2026-10-11T03:56Z: root is /dev/vda1, ext4, 98,727,608,320 bytes,
  40,736,952,320 used and 57,973,878,784 available (53.99 GiB). Root mount has
  no project-quota option. Do not change or remount root.
- The existing broad worker remains xc-mac-studio-harmony, with its current
  worker.json and multiple systemd user-unit drop-ins. Its service is
  livestack-workload-worker.service and runs worker_service with that config.
  Keep this worker, its service, and its drop-ins untouched. It has no Rust-check
  handler and no task-environment config. The authority roster read showed it
  idle; the user's Mac ARM release build is not independently mapped to it and
  remains protected.

### Additive bounded storage candidate

The supported provisioner is node-py/livestack_node/workloads/provision_workspace.py.
It creates an owned workspace image and a separate ext4 image with project and
quota features, mounted with prjquota. This works around the guest root's lack
of prjquota without changing the VM or root filesystem.

The current candidate uses an 8 GiB worker workspace and 24 GiB shared
physical-host task-environment volume. At the 03:56Z reading, the provisioner's
20 GiB disk reserve leaves 2,139,303,936 bytes (about 1.99 GiB) margin after
both images. This is close: re-read free space immediately before provisioning
and let the provisioner refuse if either reserve check fails. Do not reduce the
reserve to make it fit. The separate environment volume would have a 1 GiB
store reserve and a 23 GiB aggregate usable cap.

The existing immutable package is at /tmp/livestack-taskenv-worker-2109995,
from commit 2109995bcaa9551ad5bf6e0fd1a61d1b4744bd38, 258 files, SHA-256
93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54. It is not
staged on the Mac guest. After staging that exact package at
/home/ubuntu.guest/.local/share/livestack-workload-releases/livestack-23419ff0,
verify its hash and file count; do not rebuild it. The following is only the
provision command template after that verification and a fresh disk read:

    sudo env PYTHONPATH=/home/ubuntu.guest/.local/share/livestack-workload-releases/livestack-23419ff0/node-py /usr/bin/python3 -m livestack_node.workloads.provision_workspace --worker xc-mac-studio-harmony-rust --owner ubuntu --size-gib 8 --environment-size-gib 24 --environment-host-id xc-mac-studio

Expected workspace mount:
/var/lib/livestack-workloads/xc-mac-studio-harmony-rust/workspace.
Expected shared environment root:
/var/lib/livestack-workloads/environment-hosts/xc-mac-studio/task-environments.
The environment root must be root:ubuntu mode 01770; the sibling stable
execution view must remain empty, ubuntu-owned, and mode 0700.

Install the supported quota helper from
node-py/livestack_node/workloads/environment_quota.py at
/usr/local/libexec/livestack-task-environment-quota, root:root mode 0755.
Use /etc/livestack/workload-environment-quota.json, root-only mode 0600, with
version 1, the shared root above, max_bytes 25,769,803,776 (24 GiB), and project
IDs 100000 through 100999. Install the documented sudoers file at
/etc/sudoers.d/livestack-task-environment-quota, mode 0440, allowing ubuntu only
the helper's probe, usage, and ensure subcommands. Then run the actual positive
control from node-py/scripts/task-environment-quota-control.py as root with
owner ubuntu: require EDQUOT at the configured cap, independent usage for a
second project, and zero usage after cleanup. Also verify the real worker store
sees the separate prjquota mount and the Rust profile probes pass.

### Separate Rust-only worker and permission intersection

The proposed worker is xc-mac-studio-harmony-rust, mapped to physical host
xc-mac-studio. Do not create another physical-host row or add another 8-CPU /
16-GiB budget. Livestack placement keys and subtracts attempts by physical host;
this must remain one Mac host in accounting.

The minimum permission intersection is:

- Authority principal worker ID xc-mac-studio-harmony-rust, authenticated host
  xc-mac-studio, with only handler
  benchday.compilation.rust-check.aarch64.v1 in its handler allowlist.
- Compilation policy adds only the enrollment alias
  xc-mac-studio-harmony-rust -> xc-mac-studio. Keep the existing
  xc-mac-studio host class list and resource budget unchanged; it already
  includes rust under policy revision benchday-compilation-20261003-v4.
- Register the new ARM handler with class list [rust], Linux platform, and
  aarch64 architecture. No Flutter, image, node, native, task-E2E, full-E2E,
  dependency-E2E, or release handler is installed or authorized on this worker.
- Use a newly issued worker credential kept in the operator's protected config;
  no secret or grant bytes belong in this handoff. Give it a distinct state dir,
  workspace, and compilation launch-verifier slot.

Candidate worker config path:
/home/ubuntu.guest/.config/livestack-workloads/worker-rust.json.
Candidate separate user unit path:
/home/ubuntu.guest/.config/systemd/user/livestack-workload-worker-rust.service.
Its service must invoke worker_service with that config and the immutable staged
release. This keeps the existing broad worker's service untouched. The worker
service accepts --config, but the current rollout documentation does not define
an installer/template for a second worker on one Lima guest. Review that new
unit/config and its unique state, verifier slot, handler allowlist, and task
profile before starting it; this is an explicit human gate, not an existing
rollout API.

### ARM handler and caller-routing gate

Current handler registry generation 73 has one active default for
benchday.compilation.rust-check.v1: Linux x86_64, digest
27f65e8015d4685ca82a8931163f6a6d22042160834d5283c87a512707c77ae2. Its archive
digest is f975c65bf3bb97b773e186021ca964e17314d602ebaa4fad09d3a0aebc35294b.
All 11 registered Rust-check releases are x86_64; no ARM64 Rust-check release
exists. Handler installation requires exact platform and architecture. Preserve
the x86_64 default. Because the registry has one active default per handler ID,
ARM requires either a new handler ID plus Benchday caller selection, or a
reviewed architecture-aware registry design. Neither is implemented or
registered. This design and the second-worker unit are gates before enrollment.

After the ARM handler is registered, acceptance must use one parked handle in
this order: x86_64, aarch64, repeat aarch64, then x86_64. Verify incompatible
native caches invalidate before use, ARM succeeds, same-ABI repeat reuses only
compatible state, return to x86 rebuilds/reuses only x86-compatible state, and
all quota/cleanup receipts pass.

### Exact current worker-unit inputs after the worker became idle

The target is zz-joe-e2e-1 on physical host zz-joe. At 04:01Z, with the worker
idle, its current release path, content digest, and handler-release directory
names were re-read and verified:

- Release directory: /home/ubuntu/.local/share/livestack-workload-releases/livestack-23419ff0
- Release commit: 23419ff00ffb1f8bcd5ec829a45b5706a25d6f29
- Content SHA-256: 93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54
- Handler release root: /home/ubuntu/.local/state/livestack-workloads/zz-joe-e2e-1/handler-releases
- Handler digest count: 17; verifier: null

The exact handler digests in the post-test idle snapshot are:

  - 27f65e8015d4685ca82a8931163f6a6d22042160834d5283c87a512707c77ae2
  - 4157006f260993502c8d4447feeb58248c40a9368e8d1df3f97ea727071951bf
  - 449e0d3b41617dd91146cf0b216f7052dafdf22bf533346beb2f37149095b8b3
  - 4e1671d9c12e6c13c6fcb8eda896e0ccf3e776c92d46644c3df15bdb2ebb728d
  - 742328d4da0a9b9424fe90ee2ae1cd7ae6db2f7155b7da506101ab372ea57331
  - 8463db4f1804a9c6d836267c78da4a57ada79e948c31a342173610234179e830
  - 8747026402ce125158d8c5880817555e55ef091bfbcef75b3c11ee5103bb5d2b
  - 9be5369e10569de3f8a7992a81d636d88dc8f0205fdbe11986a6f752a65db73b
  - 9e2284fc53b217881b37cb35135d8421e6e442e4ef1ada45c108f17061587f36
  - b9c90168921b75c3f3976679c5dcebede1b681aadb07b381ab481885c3fb2079
  - c83e20a9086fe99824efc32aa140d7690796f8728be369fb6f892c594897c810
  - d027f0ff44afce825ea7cf0d16655489a0779e24ff00885de9b2630a22afcaeb
  - dfd7eb5f2d91f324d4c89632791cf28397a1be810d3b20880b8fe87257fa7390
  - dfe898eebfc3e3c9028c22c1e46e45b2d15d36711269f8ff2f0fc5ad4e2975aa
  - f50543375bddf46f439d70a0ccb64c7309493354cbfe047a574fd4cb795d578b
  - f8766965915c6b1b676376c1894f86b27cdf1482c01e6690d847d547debe6979
  - fc5f8f930a045e65d65340bd89169dcc461bc152cc69dc18f9209e257b058362

The 04:01Z authority roster historically reported worker 1 idle, ready, enabled
at claim generation 13, not draining, and with no running jobs. Livestack get returned
job not found for the previous coalesced E2E job bf5b81dbee034879a11363432f075275.
ZZOPS status reported zero pending and waiting cargo; its status.gate points to
a different train and commit, so it is not this job's verdict. The job's
pass/fail result is unavailable in this readback; do not claim one. The refreshed
handler list and directory match are recorded above. The manifest ID and digest
remain null. Capture size 0 and cap 2 GiB are only carried from the stale unit,
not remeasured. The active authority release is livestack-c2930611, while the old
unit says minimum authority c295e76a; code only validates that field as a string,
so a human must define the intended minimum. Do not guess it.

The original 02:54Z handoff reported 3448's builds as still active and a
separate 3450 job on zz-joe-release-2. Those release facts are superseded by
the owner correction at the start of this document; do not carry the 3448 hold
forward.

## Existing gates

- `openspec/changes/declarative-worker-rollout` is still phase 0–1: claims and
  observe reporting exist, but worker staging/activation, automated rollback,
  and enforce mode do not. This handoff proposes only a manually controlled
  one-worker canary; it does not approve automatic rollout.
- Livestack change `reuse-task-environments` tasks 3.2 (real admitted compiler
  and ABI invalidation) and 4.4 (worker/SDK rollout and live readback) remain
  open. Benchday selector defaults remain disabled pending the complete
  readback. Existing capability still forbids dependency/full E2E, commerce,
  and release/publishing through task environments.
- Human gates before a restart: preserve active 3450 Windows3 work; obtain an
  accepted operator identity/config; verify a fresh generation and drained,
  idle target; validate the new unit inputs; and have the operator confirm
  rollback inputs. Release 3448 is already terminal and is not a hold. No
  publish or full-suite run is part of this deployment.

## Subsequent live readback — 2026-10-11T02:59Z

The authority report advanced to generation 6211, still spec generation 2,
mode `observe`, with `applied=[]`. The desired unit and per-worker unit states
are unchanged. The 02:59:54Z roster reports all five Rust workers ready and
idle; worker 1 remains `behind`, workers 2–5 remain `unknown`, and its claim
remains enabled at generation 13. The authority's running-job list still
contains daemon Linux release job `e46fc37dabf2448cb150b0e1aa4727ab`. This
does not establish the 3450 graph state. The later owner graph correction at
the start of this document is controlling for that plan; no worker action was
taken.


## Read-only scope for this update

No VM, worker, unit, handler registry, authority, compilation policy, quota
volume/helper, task-environment config, host capacity, or automatic-selection
setting was changed. No build, test, release submission, full E2E, or publish
was started. The /tmp JSON handoff records all exact paths, digests, quota
values, and pending human gates without secrets.
