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

The worker retains existing cgroup and rootless Docker descendant supervision.
No host Docker daemon is authorized by this contract. The existing journal and
attempt outputs own bounded evidence: one current receipt per launch phase,
maximum 16 KiB, no local append-only history. Authority retention bounds remain.

Deployment introduces policy/verifier first, then consuming guards. Unclassified
handlers cannot obtain compilation launch permission. A separate remote-agent
OS boundary is required on UI-only hosts; this verifier does not claim to
constrain arbitrary root or arbitrary unguarded tools.

## Verification

Use disposable real SQLite/HTTP authority and worker/cgroup controls. Prove
eligible positive controls alongside excluded aliases, stale/missing policy,
wrong process/cgroup/input/host/fence and verifier outage refusals. Real owned
child/grandchild cleanup remains required. Never test against production.
