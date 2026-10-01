# Compilation authorization contract v1

Status: authority policy implemented; worker-local process verification and
consumer/deployment integration remain in the active change. Do not certify
script callers or excluded hosts from the authority policy alone.

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
OS confinement, compiler cleanup or end-to-end consumer rollout.
