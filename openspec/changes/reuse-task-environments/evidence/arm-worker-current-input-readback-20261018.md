# ARM Rust worker current input readback — 2026-10-18

## Scope and method

At 2026-10-18 20:54 UTC, I used SSH to `xc-mac-studio`, then the supported
Lima guest shell with `LIMA_HOME=$HOME/.lima-harmony` and `--workdir /` to read
the ARM guest's loaded user unit and immutable release metadata. The checks
did not read the private worker config contents, make an authority request,
start a job, change a worker, or touch the rollout controller. Owner-reported
policy, caller, quota, and worker state is separately labeled below.

## Exact live unit and code inputs

- Worker identity: `xc-mac-studio-harmony-rust` on guest
  `xc-mac-studio-harmony`; physical host identity remains `xc-mac-studio`.
- Loaded unit:
  `/home/ubuntu.guest/.config/systemd/user/livestack-workload-worker-xc-mac-studio-harmony-rust.service`
- Current read-only systemd state: `ActiveState=active`, `SubState=running`,
  no drop-ins. Unit SHA-256:
  `ab4eaa2aebaeef8fe9dcddb01fa82f2237a395db390a674c794ecdbe5a0a213a`.
- Loaded `ExecStart` uses
  `/home/ubuntu.guest/.local/share/livestack-workload-releases/30a89636e97ca8481d31502af06494d24435dfcc/node-py/.venv/bin/python`
  with `livestack_node.workloads.worker_service` and the private config path
  below. The runtime release `.complete` marker contains
  `5b1458f27d01d49d6563fb753b82426b3a036ed2`.
- Loaded `PYTHONPATH` is
  `/home/ubuntu.guest/.local/share/livestack-workload-releases/livestack-cfdc8d30/node-py`.
  Its `RELEASE.json` names source commit
  `cfdc8d30d0eb663ebd86f287701417cf880070c4`. A read-only recomputation over
  the 262 regular release files matched its content hash exactly:
  `5e795a4f7ec6ed608543437014a4bd2bb9c3764c0932d1e5d33cf0649450623c`.
  The `RELEASE.json` file itself has SHA-256
  `35a4256ae0f9e6a5f16a2062d48e2a706135e2b378c8ea3ba58e43f7b526ea9c`.
- The active ARM handler bundle is under
  `/home/ubuntu.guest/.local/share/benchday-harmony-handlers/0acf9314db3a9c703b307829fb02135c7a31e622`.
  Its `manifest.json` SHA-256 is
  `1b274de29d726bd82e8ea1e31b1739d7bf7b14b6efdfd5593d07d6f176cccb03`.
  The manifest names commit
  `0acf9314db3a9c703b307829fb02135c7a31e622` and exactly one handler,
  `benchday.compilation.rust-check.aarch64.v1`, with release digest
  `a0f96feed8d7c4fb0febccdbc2dc9dd1eb0abc0f9887191fa34c18ab2ab7624d` and
  archive digest
  `18faf52e39430338e5aa6026446d876f7cd7118d449270ed947a4579c6864199`.
- The handler manifest's bounded argv is
  `/usr/bin/python3 /installed/handler/scripts/harmony_rust_check.py --worker-architecture aarch64`.
  It declares the Rust-check request/result schemas, a 5,400-second limit,
  and the existing result outputs. This confirms the handler input identity;
  it does not prove caller authorization or a successful admitted ARM job.
- The private config is at
  `/home/ubuntu.guest/.config/livestack-workloads/worker-xc-mac-studio-harmony-rust.json`,
  mode `0600`, size 6,659 bytes. Its contents and digest are intentionally
  omitted. The owner previously read back that it names this worker, the ARM
  target, and only the ARM Rust-check handler.

## Stale template and manifest gap

The tracked service file in `node-py/deploy/arm-rust-worker/` is explicitly a
historical template, not the loaded unit. It uses `/usr/bin/python3`, points
both `WorkingDirectory` and `PYTHONPATH` at the `cfdc8d30` release, and says
`Restart=on-failure`. The loaded unit instead uses the pinned interpreter
release `30a89636…`, keeps `PYTHONPATH` on `cfdc8d30`, and reports
`Restart=always` / `RestartSec=5`. Do not install the tracked template or call
its hash the live unit hash.

The active handler bundle and worker code release now have exact readbacks,
but no immutable `deployment-unit.v1` manifest ID/digest was returned. The
unit-file SHA above is not that manifest. The private runtime config digest,
root verifier digest, measured capture size/cap, and manifest binding those
inputs remain `null`/unverified. The current unit implementation stores
`min_authority`; it does not enforce it at runtime. The owner-approved metadata
value is `c2930611`, tied to active authority release `c2930611`; do not use the
stale `c295e76a` value or describe it as enforcement.

The supported unit builder documented in
`node-py/docs/worker-claims-and-rollout.md` is
`python -m livestack_node.workloads.cli --config <operator-config> unit build --release-dir <release> --handlers-root <handlers> --digest <handler-digest> --verifier-dir <verified-verifier> --capture-size <measured-size> --capture-cap <verified-cap> --min-authority <owner-approved-value> --built-from <source-commit>`.
Its verifier and capture inputs are not available in this readback, so this
command was not run and no candidate manifest was fabricated. The documented
`rollout unit FILE` operation registers a unit; the controller remains
observe-only and does not install or roll back a worker.

## Minimum enrollment and remaining gates

Owner report dated 2026-10-17 says the ARM principal is enrolled by SIGHUP,
IDLE, claim OFF, and advertises exactly the ARM Rust-check handler. It is an
alias of physical host `xc-mac-studio`; do not add a second host budget. The
owner also reports the authority's active release is `c2930611`, the handler
registry generation is 149, and the ARM-only toolchain is pinned to Rust/Cargo
1.98.1. The guest has an 8 GiB workspace, a separate 16 GiB ext4 `prjquota`
volume, a 15 GiB environment maximum, and a 1 GiB store reserve; the owner
reports the quota helper's `EDQUOT` control passed. These are owner-reported
facts from 2026-10-17, not a fresh mount/quota or roster readback in this pass.

The exact ARM compiler receipts and direct `zz-joe` host-shell receipts match
on Rust 1.98.1 commit `48a229ceaefd4985c50990b14116b6d856af0985`, date
2026-09-01, LLVM 22.1.8, and Cargo 1.98.1 commit
`797e8a9bca276c1c9f9f738d2a20f484fa4eea9d`, date 2026-08-05. The x86 read is
not yet a receipt from the selected admitted x86 worker. The same-handle
x86→ARM→ARM→x86 acceptance sequence has not run.

Keep the worker claim OFF and automatic selection disabled. Before any
admitted job or unit change, the owner still needs to resolve and read back:

1. Authority `compilation_handlers` and policy for the ARM handler.
2. Caller grants limited to that ARM Rust handler and the existing `rust`
   compilation class.
3. The verifier contract/digest and measured capture size/cap, then an exact
   unit manifest ID/digest tied to the loaded interpreter, code release,
   handler release, and `c2930611` metadata.
4. A fresh target idle/cleanup guard, the same-handle four-job ABI sequence,
   and actual cleanup/quota readback.
5. The supported rollback readback for the installed ARM unit. The installed
   unit is outside any verified enforcing rollout controller; do not infer
   that observe mode can automatically roll it back.

No job was submitted, no service was restarted, no claim was enabled, and no
authority/policy/config/selection value was changed in this readback.
