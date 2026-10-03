## Context

See `proposal.md` for the motivation and `specs/github-remote-workload-execution/spec.md` for the contract. `_plans/durable-workloads.md` currently describes a claimed attempt as a local process supervised by a worker journal and cgroup. The new executor must preserve the authority's existing job, attempt, CAS, deadline, and ledger semantics while the process tree runs on GitHub-hosted macOS.

## Goals / Non-Goals

**Goals:**
- Make the workload authority the sole owner of remote attempt admission and completion.
- Represent a GitHub run as one durable and fenced attempt, including ambiguous dispatch and restarts.
- Provide a verifiable remote compilation grant and a bounded artifact path.
- Preserve the existing local and rootless-Docker execution paths unchanged.

**Non-Goals:**
- Turning GitHub Actions into an arbitrary caller-selected shell or workflow service.
- Moving product signing keys into the workload authority.
- Treating a successful GitHub conclusion alone as proof that the intended source or artifact was built.

## Decisions

### The workload authority owns remote attempt state

The existing Workload Authority SQLite store remains the durable source for jobs, attempts, reservations, dispatch intent, correlation id, remote run id and attempt number, heartbeat/status, cancellation, and artifact references. The existing CAS remains the canonical owner of source and accepted outputs. A provider loop in the authority processes a persisted dispatch outbox and reconciles in-flight attempts; it never holds a SQLite write transaction while calling GitHub. No second job database is introduced.

The authority creates the job id, attempt id, and fence. Before sending the GitHub API request it persists a unique dispatch correlation and a provider-capacity reservation. The fixed configured workflow receives those identifiers plus the immutable release commit/digest. The GitHub Actions `run_id` and `run_attempt` are persisted against that attempt after reconciliation. The runner presents an OIDC token and those identifiers when it requests a grant. Artifacts upload with an attempt-scoped token and are recorded by CAS digest. A decision-ledger record names admission/refusal, dispatch, verified runner identity, cancellation, and completion; raw credentials and OIDC tokens never enter the ledger.

### Provider identity and authority are explicit

Operator configuration maps only selected installed handlers to a GitHub provider, repository id, workflow path, approved workflow/source revision policy, runner class, compilation classes, resource vector, and maximum active slot count. The caller selects only the existing handler; it cannot supply a workflow path, repository, runner label, or API credential. The provider reads a private token file populated from this host's existing `gh auth token`; it does not require a GitHub App or a newly created external credential. A domain-separated key derived from the same local file signs attempt-scoped worker tokens, so no extra key file is needed and the GitHub token itself is never passed to the runner. The existing user token has broader `repo` scope than a dedicated App token, so it stays on the authority host. Before dispatch, the authority verifies the configured lightweight tag still points to an allowlisted workflow SHA.

The action receives `id-token: write` and exchanges a GitHub OIDC token with the authority. The authority validates GitHub's signing keys and checks repository id, workflow identity/revision, source commit, expected dispatch actor, run id/attempt, unique correlation, and the current authority fence. It reads the run's job id and status through the GitHub API and cross-checks the workflow's expected job. It returns a short-lived attempt grant. Compilation guards validate that grant against the authority before each compile step; an environment variable or generated `HARMONY_OUTPUT` directory is never sufficient evidence. No public endpoint accepts a fabricated local receipt.

The provider's virtual host is authorized by the same operator compilation
policy as local workers. Add only `github-actions-ios` with the exact iOS
handler class list; it has no physical mesh enrollment, and existing host
classes remain unchanged.

GitHub-hosted runners cannot reach the authority's Headscale-only address directly, and the current Headscale ACL is a mesh-wide allow rule. The runner therefore uses the existing HTTPS edge relay backed by the authority's outbound SSH tunnel. The relay accepts only the existing `X-Edge-Key`, `POST /v1/workloads/github/bootstrap`, the remote worker's fixed control endpoints, and the existing content-addressed object routes. Control request bodies are capped at 64 KiB; the key is stripped before forwarding. The authority still verifies GitHub OIDC during bootstrap and its attempt-scoped worker token on every later call. The runner does not join the tailnet, and no public listener is added to the authority.

`workflow_dispatch` does not provide a durable run id in its successful dispatch response. The authority therefore persists the correlation before dispatch and resolves the run through its configured workflow and correlation. A timeout with no conclusive run identity is `dispatch_unknown`: the reservation stays held while reconciliation runs, and automatic redispatch is forbidden until the first outcome is resolved. The workflow's concurrency group and the one-use authority grant prevent a duplicate from compiling even if an operator starts one manually.

### Capacity is a configured remote pool

GitHub-hosted capacity is represented as a virtual provider pool with operator-declared runner resources and a hard concurrency slot bound. Harmony reserves both the workload resource vector and a provider slot before dispatch. It does not pretend the runner is a registered physical worker with local cgroup evidence. GitHub-hosted runner limits and job timeout provide the remote VM boundary; the attempt deadline is no longer than the configured GitHub job timeout.

An unavailable provider, unknown dispatch, exhausted slot count, missing credential, or failed identity check produces a visible queued or terminal reason. There is no local or SSH fallback. Existing local handlers continue through their present scheduler and worker supervision unchanged.

### Product signing material stays in GitHub Actions

The authority's existing GitHub CLI token is used only for workflow dispatch and run inspection. The existing edge key is copied into the tag-restricted GitHub `ios-release` environment so the runner can pass the relay's transport gate; it does not authorize Harmony work by itself. iOS distribution certificate, provisioning profile, and App Store Connect API key remain in their existing GitHub secret store, are written only to per-run temporary files/keychain, masked from logs, and removed by an always-run cleanup step. They are never copied into Workload Authority state, the source snapshot, request payload, output manifest, or CAS.

### Rejected alternatives

- **Benchday directly dispatches with `gh`:** rejected because Harmony would not own capacity reservation, attempt fencing, restart reconciliation, deadline, or the completion decision.
- **Set `HARMONY_OUTPUT` or fabricate a local receipt:** rejected because it recreates the exact compile-guard bypass that stopped the old workflow.
- **Copy the iOS keys to xc-mac-studio or the authority:** rejected because the existing GitHub secret boundary already supplies the release and keeps signing credentials away from normal workload data.
- **Join the GitHub runner to this Headscale mesh:** rejected because its current wildcard ACL grants mesh-wide access, and minting a new pre-auth key would add a credential the user asked us to avoid. The existing HTTPS relay offers a narrower route without changing mesh policy.
- **Expose the workload authority publicly:** rejected because the authority remains bound to its mesh address and only the existing outbound relay can reach it. The public relay's allowlist and edge key are a bounded ingress layer, while Harmony still verifies the workload identity and token.
- **Model each ephemeral runner as a long-lived local worker:** rejected because it cannot truthfully provide a worker journal, cgroup ownership, or reliable registration identity.

## Risks / Trade-offs

- [A dispatched workflow may start while the dispatch response is lost] → Persist an outbox and correlation before network I/O, reconcile by exact workflow and correlation, hold the slot during uncertainty, and never blindly redispatch.
- [OIDC proves GitHub job identity but not an arbitrary process tree] → Pin workflow identity/revision and dispatch actor; grant only that ephemeral run; require the compile guard to revalidate against the current fence immediately before compilation.
- [GitHub runner availability, quotas, or policy may block a release] → Keep this as an explicit provider-capacity/failure reason and expose it through the existing job status; do not spend or fall back without a separately admitted provider.
- [Remote signing secrets can be exposed by a compromised workflow] → Limit the configured workflow and environment, do not grant secrets to PR/fork runs, mask values, use a one-run keychain, and always clean files and keychain state.
- [The existing CLI token is broader than a GitHub App token] → Keep it in a mode-0600 authority-only file, read it without logging, and fail closed when GitHub refuses it. Do not put it in a workload request or runner environment.
- [The public relay is probed or abused] → Require the existing edge key before forwarding, allow only fixed Harmony routes, cap control bodies at 64 KiB, keep the existing object byte budget, and preserve authority-side authentication.

## Migration Plan

Deploy the authority and relay changes with GitHub Actions provider mappings disabled. Write the existing `gh auth token` value to a mode-0600 authority credential file and copy the existing relay key into the tag-restricted `ios-release` environment; issue no new GitHub App or Headscale credentials. Configure the bounded macOS runner pool and exact iOS workflow identity, then run a build-and-sign proof with TestFlight upload disabled. Enable the iOS handler mapping only after the proof returns a source-bound artifact and receipt. Rollback disables remote admission for that handler and stops dispatch; active attempts remain visible, fenced, and reconciled to terminal state. No fallback bypass is enabled.
