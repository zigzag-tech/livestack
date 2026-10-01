## Implementation

- [x] Define bounded operator policy, canonical host identities and class contract; real authority placement/renewal tests cover alias exclusion and valid build controls; store refusal reasons on jobs. `test_workload_compilation_policy.py` plus store/HTTP/service/principal reload regressions: 83 passed on zz-joe, 2026-10-01. Contract and limitations: `node-py/docs/compilation-authorization.md`.
- [ ] Integrate policy with placement, registration and heartbeat; verify revocation fences live attempts and aliases share budgets; bounded grant receipts identify the policy revision.
- [ ] Implement worker-owned peer/cgroup authenticated launch verification and consumer CLI; real process controls reject forged environment, copied metadata and unavailable verifier; retain only current bounded receipts.
- [ ] Verify compiler/rootless builder descendants stop on cancellation/expiry before capacity release; record real supervision controls.
- [ ] Select and deploy the tested contract through normal consumer dependency workflow, preserve regression checks, and archive after companion operational evidence proves rollout.
