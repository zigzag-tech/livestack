# Live environment capability readback — 2026-10-09

Read-only `WorkloadClient.capabilities()` against the authenticated configured
authority reports schema versions `[1, 2, 3, 4]` and environment capability
version `1`.

Allowed handlers are `benchday.compilation.flutter-check.v1`,
`benchday.compilation.rust-check.v1`, and `benchday.e2e.task.v1`. The response
lists `benchday.e2e.full.v1`, `benchday.e2e.dependencies.v1`,
`benchday.commerce.report.v1`, and every `benchday.release.*` handler as
forbidden, including activation, app, CLI, daemon, hub, iOS, macOS, preparation,
staging, and web publishing.

Rust job `36de28bc5bc14f02a6cf9bcfc7deb716` was accepted with the existing
parked handle `4636512da5084b70bc2f0a1b0f045020`, generation 6. It remains
queued without attempts: workers 1, 3, and 4 lack the Rust environment profile;
worker 2 is eligible but busy; worker 5 is draining and claim-disabled. Thus
the live API policy is enabled, while broad worker enrollment and admitted
compiler evidence remain incomplete.

This endpoint readback does not identify the authority's immutable release
digest or verify live forbidden-request refusal, cleanup, or rollback. Those
items remain open. No service or worker was changed, and no full/coalesced E2E
or publishing was run.
