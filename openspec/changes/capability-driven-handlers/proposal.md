## Why

A worker serves exactly the handlers its operator typed into `worker*.json` `handlers`, and the authority serves and classifies only the ids typed into its own `handlers` and `compilation_handlers`. Three hand-kept lists, plus a per-worker root verifier for every compilation slot, plus hand-set labels. Nothing in the system knows *why* a worker does not serve a handler, so the omissions are silent. Live roster, authority DB, 2026-10-08:

| handler | serving workers (of 21 fresh) | gap that no rule explains |
|---|---|---|
| `compilation.rust-check` | zz-joe-e2e-2, -5 | e2e-1/-3/-4 are the same host, same toolchain; not listed |
| `compilation.flutter-check` | apple, wsl, zz-joe-e2e-1..5 | `xc-win-1-wsl-2` (8 cpu, 13 GiB, same image as `-wsl`) lacks it |
| `compilation.image` | mac-harmony, zz-joe-e2e-1..5 | `xc-win-1-wsl`, `-wsl-2` lack it |
| `e2e.task` | zz-joe-e2e-2 | the other six e2e-capable slots lack it |
| `release.hub`, `release.app.android` | zz-joe-release | correct (signer/keystore), but nothing says so |

Almost everything a handler needs is already known to the machine or declared in its release: operating system, CPU architecture, memory, disk, runtimes (`runtime_discovery.py`, 7637d681), backend, and a verified atomic installer (`handler_installer.py`) fed by a registry whose defaults are pushed to workers (`handler_sync`). The one thing still decided by hand is the *list*, and it is the list that is wrong. The registry cannot even offer a package to a worker whose `handlers` omits it (`desired(report['handlers'])`, and `handler_id_not_authorized_by_worker` in the installer).

Compilation handlers are different in kind: they need a root-owned, fenced verifier per worker id (`/etc/livestack/compilation-launch.json`, `harmony-compilation-<hash>.service`, `/opt/livestack-compilation/<worker>/`). Adding a slot is a root ceremony that is easy to forget, which is why a worker can be listed for a compilation handler and still refuse every launch.

## What Changes

- **Capability-driven assignment.** A worker advertises bounded *facts* (os, arch, cpu, memory, disk, runtimes, probed tools, verifier state). The authority evaluates each registry handler's *requirements* against them with one pure function and assigns the handlers the worker satisfies; the worker installs each through the existing verified installer, self-tests it, and only then serves it.
- **Requirements in three trust tiers.** Derived from the release manifest already hashed into the digest (platform, architecture, backend, runtime); package-declared hints (manifest `v2` `requirements`: memory, disk, cpu, tools; may only *narrow*); operator profile (authority config: access level, compilation classes, attested labels; the only source of any grant).
- **Three access levels.** `open` (e2e, probe, thumbnails: any matching worker), `host_enrolled` (compilation: needs operator host policy + an enrolled verifier), `attested` (signer/keystore releases: needs an operator attestation for the physical host; credentials are never distributed and the level is never auto-granted).
- **Explicit reasons everywhere.** Every worker x handler pair has one state in a roster matrix: `serving`, `eligible_not_installed`, `installing`, `selftest_pending`, `quarantined`, `ineligible(reason)`, `withheld(reason)`, `override_serving`. "Not serving X: needs memory 9 GiB, has 6 GiB" is a field, not an inference.
- **Per-host compilation verifier.** One root verifier per host serves every worker id whose principal is enrolled to that host, identity taken from the kernel peer (uid + attempt cgroup) and checked against the authority, instead of a unit, config, socket and token per worker id. An administrator-run `enroll-worker` tool generates what remains idempotently.
- **Explicit lists become overrides.** `handlers` entries are pins; a new `handler_policy.deny` is the denylist; deny > pin > computed assignment. Authority `handlers` / `compilation_classes` derive from registry profiles.
- **Observe-only first.** Compute and publish what each worker *would* serve; compare with today's lists; then enable per access level, canary on one worker.

## Capabilities

### New Capabilities
- `handler-eligibility`: facts, requirements, assignment, access levels, lifecycle, roster states, migration.
- `host-compilation-verifier`: a per-host root verifier serving enrolled worker ids, and the idempotent enrolment tool.

### Modified Capabilities
None. Additive: `workload-handler-releases` (immutable releases, atomic activation, exact inventories, bounded retention) is unchanged; assignment *uses* its install and pointer machinery and adds the `v2` manifest format beside `v1`.

## Design Record

Realises the "installed/allowlisted handlers" line in `_plans/durable-workloads.md` (L37, and the worker-config handler tables around L274-368). What is stale there: allowlisting is described as a per-worker act; after this change it is an operator *profile* plus worker-side *opt-out*. `openspec/changes/compilation-launch-authorization` (in flight) owns the per-slot verifier this change generalises; it is not edited here, its verifier contract (v1 receipt, 5 s deadline, 16 KiB) is kept.

## Impact

`node-py/livestack_node/workloads/`: `handler_release.py` (manifest v2), `handler_installer.py` (authorization gate), `runtime_discovery.py` (tool probes), `worker.py` (facts, selftest, withdrawal), `handler_registry.py` / `config.py` (profiles, `desired()`), `placement.py` unchanged, `store.py` (register accepts `facts`, validates handler ids against registry), `roster.py` + `http.py` (matrix, capacity route), `launch_verifier.py` / `launch_contract.py` (host mode), new `eligibility.py` (pure), new `enroll_worker.py` (admin). `zzops/scripts/stage_compilation_verifier.py` is superseded by `enroll_worker`. Benchday (companion change, not authored here): bundle builder emits `v2` manifests; `docs/harmony-worker-enrolment.md` shrinks to host enrolment. No live worker, authority config or service is touched by this proposal.
