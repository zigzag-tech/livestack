## ADDED Requirements

### Requirement: A release for another platform is a named skip, not an activation failure

A worker SHALL compare each offered handler release's manifest platform and architecture with its own before downloading it. A mismatch SHALL be recorded as `platform_not_supported` (handler, digest, release platform and architecture), SHALL NOT download or install the release, SHALL NOT count as a handler activation failure, and SHALL NOT stop the worker committing the registry generation. A handler named in the worker's `required_handler_releases` SHALL instead fail the activation naming `required_release_platform_missing`.

#### Scenario: Linux default offered to a macOS worker
- **WHEN** the registry default for a handler is linux/x86_64 and the worker is darwin/aarch64 without that handler in `required_handler_releases`
- **THEN** the worker commits the generation, reports one `handler_platform_skips` entry, reports no activation failure, and the roster shows `platform_skips` and no `handler_activation_failed`

#### Scenario: Required handler has no release for the worker's platform
- **WHEN** the same worker lists the handler in `required_handler_releases`
- **THEN** activation fails loudly with `required_release_platform_missing` and the generation is not committed
