## Decisions

- **Worker decides, from its own platform.** The worker already knows its exact platform and normalized architecture (`HandlerPackageStore.platform/architecture`); the authority only has free-form labels (`os=macos`, `rust_target`) that do not map one-to-one onto manifest values. Deciding at the worker avoids a lossy mapping and works against an unchanged authority for the sync itself (only the report key is new).
- **Skip before download.** The descriptor carries the manifest, so a foreign release is never fetched or installed.
- **Skipped default leaves the pointer.** The skipped handler is removed from the worker's committed defaults for that generation, so the worker's pointer advances and it keeps serving its base handler config, exactly as before any default existed.
- **Required is explicit, opt-in.** `required_handler_releases` names handlers for which "no release for my platform" must fail loudly. Nothing is hardcoded by host or handler id.
- **Placement unchanged.** Jobs of a handler carry a platform-specific selector (flutter-check selects `os=linux`); a worker that skipped the default is not given those jobs. A job pinned to a foreign digest that nevertheless reaches the worker still fails with `handler_runtime_platform_mismatch` from `handler_config`.
- **Deferred:** per-platform defaults in the registry.
