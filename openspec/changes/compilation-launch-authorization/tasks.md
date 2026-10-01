## Implementation

- [x] Define bounded operator policy, canonical host identities and class contract; real authority placement/renewal tests cover alias exclusion and valid build controls; store refusal reasons on jobs. `test_workload_compilation_policy.py` plus store/HTTP/service/principal reload regressions: 83 passed on zz-joe, 2026-10-01. Contract and limitations: `node-py/docs/compilation-authorization.md`.
- [x] Integrate policy with placement, registration and heartbeat; verify revocation fences live attempts and aliases share budgets; bounded grant receipts identify the policy revision. Real HTTP/SQLite controls in `test_workload_compilation_policy.py` and fenced launch controls prove revocation and resource limits.
- [x] Implement worker-owned peer/cgroup authenticated launch verification and consumer CLI; real process controls reject forged environment, copied metadata and unavailable verifier; retain only current bounded receipts. Root-owned framework sidecar binds the enrolled physical host and local machine-id; `test_workload_launch_verifier.py` exercises real root peers, owned cgroups, actual CPU/memory caps and current authority checks. Final policy/launch selection: 34 passed on zz-joe (2026-10-01).
- [x] Verify compiler/rootless builder descendants stop on cancellation/expiry before capacity release; record real supervision controls. Real Rust grandchild controls prove cancellation and expiry; native-frontend private image-build descendant controls prove expiry and cleanup acknowledgment; existing worker cancellation/rootless Docker regressions remain green. Broader store/HTTP/service/principal/supervision/worker/Docker selection: 160 passed on zz-joe (2026-10-01), followed by 34 final launch/policy controls after machine binding and bounded logging/receipt cleanup changes.
- [ ] Select and deploy the tested contract through normal consumer dependency workflow, preserve regression checks, and archive after companion operational evidence proves rollout.
- [x] Implement operator-reloaded claim draining without fencing live attempts; verify real HTTP heartbeats/completion, queued work and resume before using it for consumer rollout. zz-joe: 23 principal-reload, 5 HTTP/service and 11 compilation-policy controls passed (2026-10-01). Deployment remains the preceding open task.

## Multi-class launch boundary qualification (2026-10-01)

`require_compilations` verifies up to five distinct fixed classes from one
current authenticated receipt. Missing any required class refuses the whole
spawn and replaces all requested current receipts with bounded refusals.
The single-class API delegates to the same implementation; wire version stays 1.
Real disposable authority/root-peer/systemd controls on zz-joe counted exactly
one authority verification for a two-class launch, compiled and ran Rust for
the permitted control, and refused an unreserved native class before compiler
spawn. Full verifier suite: 26 passed in 48.56 seconds. Consumer archive
selection and guarded preparation integration remain pending; this does not
complete deployment or archive tasks.
