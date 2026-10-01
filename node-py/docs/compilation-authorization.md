# Compilation authorization contract v1

Status: authority policy and Linux worker-local process verification implemented;
consumer/deployment integration remain in the active change. Do not certify
remote-principal confinement or rollout from these contracts alone.

Authority configuration selects `compilation_handlers`, a mapping from installed
handler id to a nonempty list of classes (`rust`, `flutter`, `image`, `node`,
`native`), and `compilation_policy`, an absolute path to an operator-owned file.
No submission or worker tool-discovery report can set these permissions.

Example operator policy (replace identities and expiry during provisioning):

```json
{
  "version": 1,
  "revision": "operator-revision-1",
  "expires": 1800000000,
  "hosts": {
    "physical-laptop": [],
    "physical-builder": ["rust", "flutter", "image", "node", "native"]
  },
  "enrollments": {
    "laptop-enrollment": "physical-laptop",
    "laptop-second-worker-enrollment": "physical-laptop",
    "builder-enrollment": "physical-builder"
  }
}
```

Enrollments are the authenticated host fields in authority-owned worker
principals, not hostnames reported in request bodies. The operator is
responsible for mapping aliases to the enrolled physical machine. Distinct
workers sharing an identity share its resource budget. Choose physical IDs
consistent with existing persisted worker host rows when migrating; a changed
binding refuses registration rather than silently abandoning reserved capacity.

The policy file must be a regular nonsymlink file owned by root or the authority
UID, without group/world write permission. It is bounded to 16 KiB, 128 physical
hosts and 128 enrollments. It is read on each admission or verification;
install replacements atomically. Missing, expired, unsupported or unreadable
policy cannot authorize compilation. Revision changes refuse renewal of old
compilation grants; the worker's existing lease ownership performs cleanup.

Assignments carry `compilation` (null for runtime work); build grants carry
contract version, physical host, policy revision and compilation classes. The
authenticated worker route `worker/verify-compilation` requires boot,
attempt_id, fence, input_digest and class, and checks the current running
attempt without extending its lease. It returns reserved resource and input
identity. **This HTTP route does not verify an arbitrary caller's process.**
It is intended for the worker-owned local containment verifier; consumers must
not receive worker credentials or use the route as proof of their containment.

Refusals are observable in job reasons and typed HTTP errors. Existing bounded
job/attempt retention owns grant evidence; the new column is one fixed current
grant per attempt. There is no append-only policy history or local polling
service.

Validation: the disposable real HTTP/SQLite controls in
`tests/test_workload_compilation_policy.py` exercise excluded worker aliases,
prebuilt runtime eligibility, admitted builder grants, shared alias budgets,
input/worker/boot/fence/class mismatch, revision change, expiry, revocation,
missing/oversized/unsupported policy, and retry eligibility. They accompany the
existing store/HTTP/service/principal reload regressions. They do not prove
OS confinement or end-to-end consumer rollout.

## Drain before switching workers

Set `claim_enabled: false` on the selected **worker principal** in the operator
authority configuration and reload principals through the existing SIGHUP path.
New `worker/claim` requests return `{"assignment":null,"reason":"worker_draining"}`.
Fleet-wide placement also excludes the drained worker when another slot polls,
and does not count it as a retry alternative. Checking only its own HTTP claim
is insufficient because every claim invokes placement across the whole fleet.
Registration, lease renewal, compilation verification, artifact upload and
completion retain their normal fenced checks. Draining does not change a
compilation policy revision or revoke an admitted attempt. The default is true;
only boolean values are accepted, and callers/admins cannot disable claims.
Malformed reload retains the previous complete principal set.

An already authenticated request can finish using the old principal snapshot.
After reload, verify that the authority has no running/cleanup/result-handoff
attempt for the slot and the host has no owned harness process before replacing
its worker service. A completed journal alone is insufficient. Re-enable the
same principal after the compatible worker/verifier is installed; enrollment
and boot need not change to resume claims. Never remove a busy worker credential
as a substitute for drain: that also refuses renewal and completion.

The drain control stores one boolean per already bounded principal (maximum
128), with no history or new polling loop. Disposable authenticated HTTP/SQLite
controls cover renewal, canonical artifact handoff, completion, queued work,
same-boot resume, preserved live compilation verification and malformed reload.
On zz-joe, 2026-10-01: principal reload selection 23 passed; HTTP/service
regressions 5 passed; compilation-policy selection 11 passed. These tests do
not certify operational deployment.

The multi-worker placement control exposed that gap in the initial drain
implementation: it failed against captured SDK `fa886b7` and passes after the
placement intersection. The companion infrastructure-retry control also passes.
Principal/policy selection: 35 passed before adding the retry case; both final
focused cases passed (2026-10-01, zz-joe). Operational rollout must select this
placement correction before certifying a worker as drained.

## Worker-local verifier

Install `livestack_node.workloads.launch_verifier` as a root-owned service from
the selected immutable SDK. Its root-owned configuration (mode 0600) carries:

```json
{
  "version": 1,
  "worker": "enrolled-worker-slot",
  "worker_uid": 1001,
  "host": "physical-builder",
  "machine_id": "replace-with-local-32-character-machine-id",
  "authority": "http://127.0.0.1:8802",
  "token": "protected-worker-credential",
  "journal": "/var/lib/harmony/slot/active.json",
  "socket": "/run/harmony-launch-slot/verify.sock"
}
```

The numerical authority address, configured machine-id and enrolled canonical
host bind the service to this worker execution environment. For a VM, the
operator maps the guest's enrolled identity to its physical host policy/budget;
the local machine-id pins the execution guest as well. Socket directories must
be root-owned and unwritable by callers. A service restart must occur after the
previous listener stops; a leftover socket refuses a second listener.

`/etc/livestack/compilation-launch.json` is a root-owned registry (at most 16 KiB,
128 slots) with `{"version":1,"slots":{"enrolled-worker-slot":"/run/harmony-launch-slot/verify.sock"}}`.
It cannot be replaced through a writable parent directory. The consumer
authenticates the connected server's kernel root identity, rather than trusting
an environment-selected endpoint. The root verifier uses kernel peer PID/UID,
actual systemd cgroup containment, process start identity, actual CPU/memory
caps and a fresh authority check. The credential stays in the root service.

Worker configuration selects `compilation_launch_contract: 1`; admitted build
handlers receive authoritative job/attempt/boot/fence/input/host/policy metadata
in their environment. `python -m livestack_node.workloads.launch_guard --class
rust` verifies the invoking process and refuses unmanaged callers. Unknown or
unsupported contracts do not receive permission. Metadata and receipts are not
reusable credentials.

Use handler backend `rootless-docker-native` for guarded image builders. Its
native frontend can authenticate the host root verifier while Docker's daemon
and build containers stay inside the owned RootlessKit subtree. The frontend
proves its private daemon's PID/cgroup and data-root endpoint and clears inherited
Docker context overrides. This avoids weakening root authentication inside a
user namespace. Legacy `rootless-docker` remains a runtime backend; its user
namespace cannot run this host-namespace guard.

Both Docker backends classify exit 75 as infrastructure, preserving bounded
retry and diagnostic handoff instead of reporting a product assertion failure.
The real worker artifact-delivery control covers both backends with success,
product failure and exit 75, using private authorities, canonical captured inputs,
actual systemd ownership and private Docker daemons. All six cases passed on
zz-joe on 2026-10-01; no production job was submitted for these controls.

Bounds and enforcers:

- `launch_contract.receive` and root service SIGALRM: absolute 5-second
  verification deadline, 16 KiB messages; `authority_receipt` bounds read bytes.
- `launch_guard.write_current_receipt`: five finite class names, one current
  16 KiB JSON receipt per class, at most five fixed temp names and five empty
  lock files; atomic replacement and nonblocking locks. A failed verification
  replaces stale success evidence with a named refusal. Existing attempt
  workspace/artifact retention owns deletion.
- Root verifier `RotatingFileHandler`: one 2 MiB log and two backups per slot.
- Native Docker readiness: one current <=1 KiB record per attempt; native
  frontend bounds reads and authenticates PID/cgroup. Existing verified cgroup
  stop plus `docker_runtime.remove_data` owns private-layer cleanup.

`tests/test_workload_launch_verifier.py` uses a disposable root service and real
authority/systemd/cgroups. Its positive controls actually compile/run Rust and
build a private scratch image. Negative controls cover forged/copy metadata,
wrong worker/host/input/fence/cgroup/resource caps, a copied configuration on
the wrong machine identity, unavailable/forged socket, unsupported contract,
revoked/cancelled/expired admission, oversized replies and deadlines. Live
Rust compiler grandchildren and image-builder descendants stop on actual
lease expiry; capacity cannot be reused until verified cleanup is acknowledged.
