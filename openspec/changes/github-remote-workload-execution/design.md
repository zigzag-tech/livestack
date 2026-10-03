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

Operator configuration maps only selected installed handlers to a GitHub App installation, repository id, workflow path, approved workflow/source revision policy, runner class, compilation classes, resource vector, and maximum active slot count. The caller selects only the existing handler; it cannot supply a workflow path, repository, runner label, or API credential. The configured App credential is an operator-owned file with Actions write and Contents read permission for the one release repository. Before dispatch, the authority verifies the configured lightweight tag still points to an allowlisted workflow SHA.

The action receives `id-token: write` and exchanges a GitHub OIDC token with the authority. The authority validates GitHub's signing keys and checks repository id, workflow identity/revision, source commit, expected dispatch actor, run id/attempt, unique correlation, and the current authority fence. It reads the run's job id and status through the GitHub API and cross-checks the workflow's expected job. It returns a short-lived attempt grant. Compilation guards validate that grant against the authority before each compile step; an environment variable or generated `HARMONY_OUTPUT` directory is never sufficient evidence. No public endpoint accepts a fabricated local receipt.

GitHub-hosted runners cannot reach the authority's current tailnet-only address unaided. The workflow first joins the tailnet through Tailscale workload identity federation, which trusts the approved GitHub OIDC repository/workflow claims and creates an ephemeral `tag:benchday-ios-gh` node. Tailnet ACLs permit that tag to reach only the workload authority API on its configured port. The same job then presents a separate GitHub OIDC token to Harmony for attempt authorization. No static Tailscale auth key or public workload-authority listener is introduced.

`workflow_dispatch` does not provide a durable run id in its successful dispatch response. The authority therefore persists the correlation before dispatch and resolves the run through its configured workflow and correlation. A timeout with no conclusive run identity is `dispatch_unknown`: the reservation stays held while reconciliation runs, and automatic redispatch is forbidden until the first outcome is resolved. The workflow's concurrency group and the one-use authority grant prevent a duplicate from compiling even if an operator starts one manually.

### Capacity is a configured remote pool

GitHub-hosted capacity is represented as a virtual provider pool with operator-declared runner resources and a hard concurrency slot bound. Harmony reserves both the workload resource vector and a provider slot before dispatch. It does not pretend the runner is a registered physical worker with local cgroup evidence. GitHub-hosted runner limits and job timeout provide the remote VM boundary; the attempt deadline is no longer than the configured GitHub job timeout.

An unavailable provider, unknown dispatch, exhausted slot count, missing credential, or failed identity check produces a visible queued or terminal reason. There is no local or SSH fallback. Existing local handlers continue through their present scheduler and worker supervision unchanged.

### Product signing material stays in GitHub Actions

The authority's GitHub App key is used only for workflow dispatch and run inspection. iOS distribution certificate, provisioning profile, and App Store Connect API key are injected into an authorized GitHub Actions environment as secrets, written only to per-run temporary files/keychain, masked from logs, and removed by an always-run cleanup step. They are never copied into Workload Authority state, the source snapshot, request payload, output manifest, or CAS.

### Rejected alternatives

- **Benchday directly dispatches with `gh`:** rejected because Harmony would not own capacity reservation, attempt fencing, restart reconciliation, deadline, or the completion decision.
- **Set `HARMONY_OUTPUT` or fabricate a local receipt:** rejected because it recreates the exact compile-guard bypass that stopped the old workflow.
- **Copy the iOS keys to xc-mac-studio or the authority:** rejected because the existing GitHub secret boundary already supplies the release and keeps signing credentials away from normal workload data.
- **Expose the workload authority publicly or add a long-lived Tailscale key to GitHub:** rejected because ephemeral identity federation and a narrow tailnet ACL give the approved remote job the private route it needs without expanding authority exposure or storing another static credential.
- **Model each ephemeral runner as a long-lived local worker:** rejected because it cannot truthfully provide a worker journal, cgroup ownership, or reliable registration identity.

## Risks / Trade-offs

- [A dispatched workflow may start while the dispatch response is lost] → Persist an outbox and correlation before network I/O, reconcile by exact workflow and correlation, hold the slot during uncertainty, and never blindly redispatch.
- [OIDC proves GitHub job identity but not an arbitrary process tree] → Pin workflow identity/revision and dispatch actor; grant only that ephemeral run; require the compile guard to revalidate against the current fence immediately before compilation.
- [GitHub runner availability, quotas, or policy may block a release] → Keep this as an explicit provider-capacity/failure reason and expose it through the existing job status; do not spend or fall back without a separately admitted provider.
- [Remote signing secrets can be exposed by a compromised workflow] → Limit the configured workflow and environment, do not grant secrets to PR/fork runs, mask values, use a one-run keychain, and always clean files and keychain state.

## Migration Plan

Deploy the authority and schema changes with GitHub Actions provider mappings disabled. Configure the least-privilege GitHub App, bounded macOS runner pool, and exact iOS workflow identity. Re-enable the iOS workflow with OIDC and secret cleanup, then run a build-and-sign proof with TestFlight upload disabled. Enable the iOS handler mapping only after the proof returns a source-bound artifact and receipt. Rollback disables remote admission for that handler and stops dispatch; active attempts remain visible, fenced, and reconciled to terminal state. No fallback bypass is enabled.
