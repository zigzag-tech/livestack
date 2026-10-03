# GitHub Actions workload provider operations

This runbook applies to the `github-actions` provider in LiveStack's durable
workload authority. Harmony remains the scheduler and source of truth; GitHub
supplies one macOS runner for a fixed admitted attempt. The hosted runner does
not join Headscale and does not receive the authority's GitHub API credential.

## Existing identities and credentials

The authority uses the current `gh` login on xc-tower-ubuntu. Refresh the
authority-local token file without printing its value:

```bash
umask 077
tmp="$HOME/.config/livestack-workloads/github-token.next"
gh auth token > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$HOME/.config/livestack-workloads/github-token"
```

The service checks that the file is regular, private, and owned by the service
user or root. The provider reads it for GitHub API calls. The attempt-worker MAC
key is domain-separated from the same file, so no separate key or GitHub App is
needed. A replacement token changes that derived key on the next service start;
rotate it only while the provider has no active remote attempt.

The runner reaches the authority through
`https://hs.zztech.io/harmony-relay`, which is the existing HTTPS edge relay
backed by `harmony-edge-tunnel.service` and its outbound SSH reverse tunnel.
Copy the existing `key` from
`~/.config/livestack-workloads/edge-relay.json` into the GitHub
`ios-release` environment secret `HARMONY_EDGE_KEY`. Do not copy its admin
token. The edge key gates relay transport only: Harmony still validates the
GitHub OIDC bootstrap and the attempt-scoped worker token.

The relay forwards only `POST /v1/workloads/github/bootstrap`, the fixed
`/v1/workloads/worker/{status,report,claim,heartbeat,verify-compilation,complete}`
routes, and the existing content-addressed object routes. Every forwarded
request requires `X-Edge-Key`; the relay strips that header before forwarding.
Control bodies are limited to 64 KiB. Other routes are refused at the relay,
and object traffic remains subject to its monthly byte budget. The workload
authority stays bound to `100.64.0.18:8810` on Headscale.

The GitHub worker's source and artifact transfers use `object_relay` pointed at
the same public relay URL and authenticated with the existing
`HARMONY_EDGE_KEY`. The attempt-scoped worker token still authenticates the
transfer to Harmony; the edge key only admits the relay hop. Keep both headers
on relay object requests so the relay can strip its key before forwarding.

The six existing Apple signing values remain repository-level GitHub secrets;
their encrypted values cannot be moved into an environment without their
original plaintext source. The fixed workflow references them only from the
tag-restricted `ios-release` environment and imports them into per-run signing
state. The relay key is an environment secret. Do not claim that the environment
scope changes the storage scope of the existing Apple secrets.

## Pin the workflow

Use a new lightweight release-workflow tag for every security-sensitive change;
never move an existing tag. The current target is
`benchday-ios-remote-v7`. The authority verifies that its commit SHA is on the
allowlist before dispatch. Set both `workflow_ref` values to:

```
settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v7
```

The provider's `workflow_path` is
`.github/workflows/release-ios-harmony.yml`; its `workflow_id` is the fixed
numeric ID returned by GitHub. Identity configuration also pins the repository
id, workflow id, exact tag commit SHA, `workflow_dispatch`, job name,
correlation prefix, audience `harmony`, and the approved actor id (the existing
`settinghead` account). Before dispatch, the authority confirms the tag is a
lightweight tag pointing directly to an allowlisted commit. GitHub OIDC
encodes `run_attempt` as a canonical decimal string; the verifier normalizes
it before comparing against the API run record, which uses an integer.

The GitHub `ios-release` environment must permit only the current tag and have
administrator bypass disabled. Its `BENCHDAY_GITHUB_ACTOR_ID` variable matches
the existing GitHub CLI user's numeric actor id. The job-level actor, tag, and
first-attempt gate runs before the workflow reads signing values or the relay
key. Keep `id-token: write`, `actions: read`, and `contents: read` as the only
workflow token permissions.

## Authority configuration

Add the provider mapping without replacing existing handlers, principals,
compilation policy, or local workers. Both provider token paths point to the
existing private CLI credential file; LiveStack derives a separate MAC key
with domain separation. Replace the example identity values with values from
the existing GitHub repository and the landed tag; do not print credentials.

```json
{
  "github_remote": {
    "token_key_file": "/home/ubuntu/.config/livestack-workloads/github-token",
    "interval": 5,
    "providers": {
      "github-actions": {
        "handlers": ["benchday.release.ios.v1"],
        "host": "github-actions-ios",
        "resources": {"cpu": 3, "memory_bytes": 9663676416, "disk_bytes": 12884901888},
        "labels": {"os": "macos", "signing": "apple"},
        "slots": 1,
        "workflow_path": ".github/workflows/release-ios-harmony.yml",
        "workflow_ref": "settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v7",
        "workflow_id": 12345678,
        "identity": {
          "repository": "settinghead/benchday",
          "repository_id": "REPOSITORY_ID",
          "workflow_ref": "settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v7",
          "workflow_id": 12345678,
          "workflow_sha": ["ff7f825d2cd3a71e42bff49adf9c64dddc2ae9a9"],
          "event_name": "workflow_dispatch",
          "job_name": "Build and upload iOS",
          "audience": "harmony",
          "correlation_prefix": "Harmony iOS release ",
          "actor_ids": ["794516"]
        },
        "token_file": "/home/ubuntu/.config/livestack-workloads/github-token",
        "max_seconds": 21600,
        "compilation_classes": ["apple", "flutter", "native", "rust"]
      }
    }
  }
}
```

The numbers describe the fixed `macos-26-intel` runner envelope, not a general
Mac pool: one slot, 3 CPU, 9 GiB memory, and 12 GiB disk. The configured
compilation classes must exactly match the handler's `compilation_policy`.
Keep the workflow's APFS workspace quota and reserves within the hosted runner
profile.

Keep launchd's per-attempt plist and execution config in
`$RUNNER_TEMP/worker-state/launchd`, on the runner's system-backed temporary
filesystem. The bounded source and build output remain on the mounted APFS
workspace; the wrapper reads its working directory from the private execution
config. Do not point launchd's `WorkingDirectory` or plist path into the mounted
image.

The authority's global CAS bounds apply to release source archives and returned
artifacts as well as E2E objects. Current limits are 140 GiB total, 8 GiB per
object, 8,192 objects and 24-hour retention for unreferenced objects. Check CAS
usage and free host disk before increasing a bound; preserve references and the
retention window when a release source is refused for capacity.

The authority's external `/etc/livestack/compilation-policy.json` must also
contain a `github-actions-ios` host entry granting exactly
`apple`, `flutter`, `native`, and `rust`. The provider maps this virtual
host directly, so it does not need a mesh-worker enrollment. Do not add it to
the physical-worker enrollment map or widen any existing host; preserve the
current expiry and advance the policy revision when adding this entry.

## Rollout and checks

1. Run the LiveStack workload tests and `openspec validate --specs` before
   deployment. Stage an immutable authority release with the provider and
   relay code. Preserve the current workload state directory and database.
2. Verify `gh auth status` and the non-secret GitHub identity, then write the
   existing CLI token to its private local file. Copy the existing edge key to
   `HARMONY_EDGE_KEY` in the tag-restricted environment. Do not create an App,
   Headscale node, pre-auth key, or Tailscale federation secret.
3. Back up the authority config and compilation policy. Add the exact provider
   mapping and the matching virtual-host compilation grant. Keep all current
   local worker entries and physical-host enrollments. Repoint the staged
   service to the new immutable release only at an idle, result-safe point,
   then restart and verify workers, principals, and stored jobs are intact.
4. Verify the edge relay returns its public liveness status. From the relay
   tests, confirm unlisted routes, missing keys, and control bodies over 64 KiB
   stop before forwarding, and that the edge key is stripped. The authority
   remains inaccessible outside its private Headscale address.
5. Use `publish-ios.sh --no-upload` as the hosted macOS proof. Confirm Harmony
   binds the live run and job, issues one fenced token, returns a digest-verified
   IPA, cleans signing material, and reconciles the terminal state. Confirm no
   local or Mac worker compiled the iOS handler.
6. Only after that proof, enable the normal iOS upload path. Verify App Store
   Connect acceptance and the source/build/run provenance before the publisher
   updates the cadence ledger.

If GitHub or the relay is unavailable, the provider fails closed and the
attempt remains visible in Harmony. Diagnose its workload reason before
retrying. Do not bypass OIDC, use an unrestricted mesh route, or fall back to a
local or Mac build.
