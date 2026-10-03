# GitHub Actions workload provider operations

This runbook applies to the `github-actions` provider in LiveStack's durable
workload authority. The authority remains the scheduler and source of truth.
GitHub only supplies a one-job runner. Do not route a release handler here
until the identity, network, resource and sandbox checks below pass.

## Required identities and permissions

Create a GitHub App installed only on `settinghead/benchday` with Actions write
and Contents read permissions (repository metadata read is implicit). Generate
one App private key and place it on the authority host in a root/authority-owned file
with mode `0600`; keep only its path in configuration. The App installation
token is minted by the authority for dispatch and run reconciliation. It is
never passed to a runner.

Record these immutable repository values with the GitHub API: repository id,
workflow id, and the App bot's numeric actor id. The repository id prevents a
renamed or recreated repository from inheriting the route. The workflow id
must identify `.github/workflows/release-ios-harmony.yml`.

Protect the `ios-release` GitHub environment and keep the existing Apple
distribution certificate, profile and App Store Connect API key secrets there
when they are available as environment secrets. The workflow's job-level gate
also requires the configured App actor, the one approved tag ref and run
attempt 1. Review that gate whenever the workflow changes. Never print secret
values or copy them into the authority, workload database, artifacts or logs.

## Pin the workflow

Create a lightweight tag `benchday-ios-remote-v1` only after the workflow and
runner scripts have landed on `main`. The authority verifies the tag points
directly to an allowlisted commit before it dispatches. Record the tag's commit SHA in the
authority's `identity.workflow_sha` allowlist and set `identity.workflow_ref`
to:

```
settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v1
```

The provider's `workflow_ref` must name the same repository, file and tag. Its
`workflow_path` is `.github/workflows/release-ios-harmony.yml`, and its
`workflow_id` is the numeric id returned by GitHub. If the workflow needs a
security-sensitive change, publish a new tag, then update the allowed SHA and
ref together. Do not move an existing tag or permit arbitrary branch refs.

## Authority configuration

Add a `github_remote` object to the authority config without replacing the
existing handlers, principals, compilation policy or local workers. Keep the
token key and App private key outside the checkout, readable only by the
authority process. The required shape is:

```json
{
  "github_remote": {
    "token_key_file": "/etc/livestack/github-worker-token.key",
    "interval": 5,
    "providers": {
      "github-actions": {
        "handlers": ["benchday.release.ios.v1"],
        "host": "github-actions-ios",
        "resources": {"cpu": 3, "memory": 9663676416, "disk": 12884901888},
        "labels": {"os": "macos", "signing": "apple"},
        "slots": 1,
        "workflow_path": ".github/workflows/release-ios-harmony.yml",
        "workflow_ref": "settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v1",
        "workflow_id": 12345678,
        "identity": {
          "repository": "settinghead/benchday",
          "repository_id": "REPOSITORY_ID",
          "workflow_ref": "settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v1",
          "workflow_id": 12345678,
          "workflow_sha": ["40_HEX_TAG_COMMIT_SHA"],
          "event_name": "workflow_dispatch",
          "job_name": "Build and upload iOS",
          "audience": "harmony-workload-bootstrap",
          "correlation_prefix": "Harmony iOS release ",
          "actor_ids": ["GITHUB_APP_BOT_ACTOR_ID"]
        },
        "app": {
          "app_id": 123456,
          "installation_id": 987654,
          "private_key_file": "/etc/livestack/benchday-github-app.pem"
        },
        "max_seconds": 21600,
        "compilation_classes": ["apple", "flutter", "native", "rust"]
      }
    }
  }
}
```

The numbers above describe the `macos-26-intel` runner envelope used by the
workflow, not a general Mac worker pool: one slot, 3 CPU, 9 GiB memory, and
12 GiB disk. The authority clamps its worker report to those configured
resources. The configured compilation classes must exactly match
`compilation_policy` for this handler. Review the workflow's APFS workspace
quota and reserve against the current hosted runner profile before raising any
limit.

Set the workflow bootstrap audience to the exact configured audience. Configure
Tailscale Workload Identity Federation to accept only this repository, the
`ios-release` environment, and the approved workflow identity. The resulting
short-lived runner identity may reach only the workload authority bootstrap
and worker API on the `github-actions-ios` tag. Its ACL must not grant SSH,
database, hub administration, or access to other tailnet services. The
authority API must not expose the bootstrap route outside the tailnet.

## Rollout and checks

1. Run the LiveStack workload tests and `openspec validate --specs` before
   deployment. Install the immutable release on the authority host using its
   normal release mechanism; preserve the current state directory and database.
2. Back up and inspect the authority config. Add the provider mapping, secret
   file paths, exact compilation classes and repository/workflow allowlists.
   Do not remove or change the current local worker entries.
3. Restart the authority using its service manager. Confirm it loads the
   provider and retains its existing workers, principals and job rows. Verify
   the GitHub App can mint an installation token and list the pinned workflow,
   without logging the token.
4. Dispatch a sandbox workflow that uses the same OIDC, GitHub API and
   Tailscale path but does not receive Apple secrets or run a release handler.
   Confirm the authority binds the live run and job id, issues a single-use
   fenced credential, accepts a small digest-verified artifact, and reconciles
   cancellation and completion. Repeat with a manual ref, fork, wrong actor,
   wrong workflow SHA, rerun and expired grant; each must be rejected before
   worker claim or secret use.
5. Inspect the workload ledger for provider, correlation, run/attempt,
   identity decision, artifact digest and terminal result. Confirm the
   one-runner slot remains occupied until GitHub terminal cleanup, and that
   the local Mac workers continue to receive their existing jobs.
6. Only then route `benchday.release.ios.v1` to GitHub and use the publisher's
   `--no-upload` path as the non-publishing release proof. Verify signing
   cleanup, source/build provenance, IPA receipt and TestFlight upload before
   recording the cadence ledger.

If any GitHub API or identity check is unavailable, the provider fails closed:
the job remains durable and capacity is not silently represented as a local
worker. Diagnose the provider's own reason and reconcile before retrying. Do
not bypass OIDC, turn on a local runner, or place Apple credentials on the Mac
as a recovery path.
