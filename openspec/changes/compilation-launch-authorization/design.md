## Context

Real existing placement and systemd ownership are the base. Installed tools and
report labels do not grant permission. Authenticated worker principals already
bind enrollment host identity, preventing a caller from supplying it in JSON.

## Decisions

The authority owns a versioned operator policy with a revision and expiry,
bounded handler-to-class mappings and host enrollment-to-physical-id mappings.
Multiple aliases for one physical identity share a single class policy and
resource budget. Missing/expired mappings cannot grant a compilation class.
No client may supply these policy rows through a workload submission.

Authority grants bind physical host, policy revision, handler-required classes,
input digest, worker boot, job/attempt/fence and reserved resources. Heartbeats
revalidate current policy; revocation terminates renewal instead of permitting
further launches. The worker owns one Unix socket in a protected runtime
directory and uses SO_PEERCRED plus the owned systemd cgroup to verify invoking
process containment. A five-second deadline and 16 KiB request/response bound
apply. The worker checks current authenticated authority state for each launch.
The caller has no authority credential and receives a receipt, never a bearer
credential reusable outside its attempt. A copied environment is insufficient.

The local verifier is a root-owned framework sidecar for one worker slot. Its
root-owned configuration binds the slot, compiler UID, enrolled physical host,
local machine-id and protected authority credential. A root-owned registry
selects its Unix socket; the client authenticates the connected root peer using
SO_PEERCRED. Caller environment cannot select a fake verifier or registry.
The sidecar compares the live journal and authority receipt, checks the actual
systemd attempt cgroup and its memory/CPU caps, and rechecks peer process start
identity and containment after the bounded authority request. A copied sidecar
configuration cannot start on a different enrolled OS machine identity.

Rootless user namespaces change the apparent UID of the root verifier. Rather
than accepting an ambiguous namespace UID, `rootless-docker-native` keeps the
installed compilation frontend in the host user namespace and runs only its
private Docker daemon and build containers under RootlessKit. The native
frontend authenticates the private daemon's PID/cgroup and endpoint before use;
all components remain beneath the same delegated attempt cgroup. It never
selects the host Docker daemon or trusts inherited Docker contexts.

The worker retains existing cgroup and rootless Docker descendant supervision.
No host Docker daemon is authorized by this contract. The existing journal and
attempt outputs own bounded evidence: one current receipt per launch phase,
maximum 16 KiB, no local append-only history. Authority retention bounds remain.

Deployment introduces policy/verifier first, then consuming guards. Unclassified
handlers cannot obtain compilation launch permission. A separate remote-agent
OS boundary is required on UI-only hosts; this verifier does not claim to
constrain arbitrary root or arbitrary unguarded tools.

For a safe worker transition, an operator can reload a worker principal with
`claim_enabled: false`. This pauses new claims with the explicit response reason
`worker_draining`; it preserves its authenticated registration, heartbeat,
compilation verification, completion and artifact transfer. It does not revoke
already admitted work or change policy revisions. Requests already authenticated
before reload may finish under the old principal snapshot, so deployment still
requires the two-layer idle check after draining. Re-enabling the same principal
resumes claims without changing its boot or enrollment. Only worker principals
can disable claims; malformed/non-boolean settings refuse configuration reload.
The control adds one boolean per existing bounded principal, no retained history
or polling service. Real HTTP/SQLite controls must prove handoff and resume.

## Verification

Use disposable real SQLite/HTTP authority and worker/cgroup controls. Prove
eligible positive controls alongside excluded aliases, stale/missing policy,
wrong process/cgroup/input/host/fence and verifier outage refusals. Real owned
child/grandchild cleanup remains required. Never test against production.
