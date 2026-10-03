## Why

Harmony currently dispatches accepted work to installed native or rootless-Docker workers, while some valuable jobs only run on GitHub Actions, including the previous Benchday iOS release. That route was retired when local compiler admission became mandatory, leaving no supported way for Harmony to authorize and durably supervise a remote GitHub run.

This realizes `_plans/durable-workloads.md`. That record assumes a claimed attempt becomes a locally supervised process under a worker cgroup; the remote-provider case is stale. Local execution remains supported, while a remote run must become the same fenced, source-bound Harmony attempt rather than an untracked side job.

## What Changes

- Add a GitHub Actions execution provider for configured workload handlers, with bounded provider capacity and durable dispatch, run identity, status, cancellation, and completion under the existing workload authority.
- Bind each remote run to one admitted job attempt, fence, immutable input digest, fixed repository and workflow identity, and absolute deadline. Authenticate the runner with GitHub OIDC and issue only a short-lived attempt grant that the remote compilation guard can verify.
- Let only the approved GitHub workflow join the tailnet through Tailscale workload identity federation, using an ephemeral tagged runner with ACL access only to the workload authority API. This gives the runner a private route without a long-lived tailnet key or a public authority endpoint.
- Verify remote logs and output objects through the authority's existing size, ownership, digest, and source-provenance rules. Stale, duplicate, manually dispatched, or mismatched runs cannot complete an attempt or return accepted artifacts.
- Keep provider credentials and workload state outside job payloads. The authority's narrowly scoped GitHub App credential stays in operator configuration; product signing secrets remain in the configured GitHub Actions secret store and never enter Harmony payloads or CAS.
- Do not silently run a provider-selected job on another backend when GitHub is unavailable, over capacity, or cannot prove run identity.

## Capabilities

### New Capabilities
- `github-remote-workload-execution`: Harmony's admission, authorization, supervision, and result contract for jobs executed by GitHub Actions.

### Modified Capabilities

## Impact

Affected areas include `node-py/livestack_node/workloads/` (submission policy, placement, attempt lifecycle, provider integration, and result validation), workload authority configuration and deployment, workload tests, and `_plans/durable-workloads.md`. The API remains backward compatible for existing local-worker callers; remote execution is enabled only for explicitly configured handlers and repository/workflow identities.
