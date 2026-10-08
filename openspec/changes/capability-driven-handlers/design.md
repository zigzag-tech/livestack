## Context

Where durable state lives today, and who owns it:

| State | Owner | Today |
|---|---|---|
| Installed handler ids | authority config `handlers` (add-only on SIGHUP) | typed |
| Compilation classes per handler | authority config `compilation_handlers` | typed |
| Host classes, enrolment aliases | operator policy file (`compilation_policy`, read per decision) | typed, per **physical host** |
| Release catalogue, defaults, generations | authority SQLite (`HandlerReleaseRegistry`) | staged by operator CLI |
| Which handlers a worker serves | worker `worker*.json` `handlers` (with argv, backend, `max_seconds`, `max_tasks`, exit codes) | typed, per worker |
| Runtimes | worker `handler_runtimes`, now overridden by discovery (7637d681) | discovered |
| Labels (`android_sdk`, `android_signer_sha256`, `region`, `rust_target`...) | worker config `labels` | typed, unverified |
| Compilation launch fence | root unit + `verifier.json` + registry entry, **per worker id** | staged by `zzops/scripts/stage_compilation_verifier.py` |

Ids cross boundaries as: worker -> authority `worker/report` (`handlers`, `labels`, `capacity`, `handler_inventory`); authority -> worker `handler_sync` (`defaults`, `releases`). Placement reads only `report.handlers` and `report.labels`. This change keeps that surface: **placement is untouched**; what changes is who decides the contents of `report.handlers`.

## Goals / Non-Goals

**Goals.** A worker serves every handler whose requirements it satisfies and installs it itself; adding a worker or slot on an enrolled host needs no list edit; every absence has a named reason; privileged capability is never granted by anything a worker or a package says about itself.

**Non-Goals.** Changing placement, admission or the lease protocol. Distributing credentials, keystores or signing keys (never). Choosing models for GPU engine units (Harmony-engine; separate). Auto-provisioning toolchains (rustup, Android SDK, Flutter): they are *facts to detect*, not things the worker installs, except handler packages. Cross-OS emulation.

## The hard question first: why is the compilation verifier per worker id, and can it be per host?

### What the fence actually binds (read from `launch_verifier.py` / `launch_contract.py`, and `/opt/livestack-compilation/*/verifier.json` on zz-joe, token redacted)

A `verifier.json` carries: `worker`, `worker_uid`, `host`, `machine_id`, `authority`, `token`, `journal`, `socket`. A launch request is `{worker, host, policy_revision, boot, job_id, attempt_id, fence, input_digest, class}`; **every field comes from environment variables the worker put in the handler's env (`environment_request`)**. None is a secret and none is signed. So the fence is *not* "trust the request"; per request the verifier checks, in `verify_peer`:

1. `SO_PEERCRED`: peer uid == `worker_uid` (kernel fact). `request.worker == config.worker`, `request.host == config.host`.
2. The worker's own journal (`config.journal`) is `phase=running` and its assignment equals the request field by field (a cross-check, not a trust anchor: the journal is writable by the worker's uid).
3. The peer's cgroup is inside the systemd unit `harmony-work-sha256(worker)[:16]-<attempt>.service` that is `active` (kernel fact: where the process lives).
4. **The authority** (`POST worker/verify-compilation`, authenticated by the worker's **token**; the authority takes the worker id *from the token*, never from the body) says attempt/boot/fence/input/class are live and reserved for that worker, with the reserved `execution_resources`.
5. The cgroup's real `memory.max` / `cpu.max` are no larger than the reservation (kernel fact).
6. Peer identity and unit are re-read after the authority hop; any change refuses (TOCTOU).
7. `machine_id` pins the config to this machine; the consumer authenticates the *server* as uid 0 via `SO_PEERCRED` before sending anything.

So the property the fence protects is: **a compilation receipt is minted only for a process that lives inside a live, resource-capped attempt that the authority granted to a worker on an operator-allowed host for that class.** What it prevents: (a) a non-compilation job or foreign process obtaining a receipt (the 2026-10 incident class: `compilation_launch_outside_harmony_attempt`); (b) a handler asserting another worker's identity or host to borrow its classes; (c) a receipt reused after the attempt ends; (d) a verifier config copied to another machine; (e) a handler exceeding its reservation.

### What is, and is not, per-worker about it

Observed on zz-joe: **all six slots have `worker_uid: 1000`**. The six verifiers differ only in `worker`, `journal`, `token`, `socket`. Therefore:

- The kernel cannot tell zz-joe-e2e-1's handler from zz-joe-e2e-3's by uid. The cross-worker separation that exists is (3)+(4): the unit-name hash and the token-authenticated authority answer. Both are *data keyed by worker id*, not by process.
- A handler in worker B's attempt that claims `worker=A` is refused today because A's verifier requires its peer in A's unit (it is in B's), and A's authority answer would not match. Looking up row A in a shared table and running the same checks refuses it identically. **No property is lost by moving the worker id from unit level to request level**, provided the id selects a row of *root-enrolled* data and every kernel check stays.
- A **signed or attested worker identity in the request is rejected**: the secret that would sign it must live where the worker (and, on a shared uid, its handlers) can read it. The worker's token already is such a secret and is deliberately kept in the root verifier. Kernel facts (uid, cgroup) are the only attestation a handler cannot forge.

### What actually needs root, per worker or per host

| Datum | Security-relevant? | Who may supply it |
|---|---|---|
| `worker_uid` (account whose attempts are trusted) | **Yes.** This is the root-asserted claim "attempts run as this uid". If a user could add it, any local account could create a unit named like a victim's attempt (attempt ids are not secret on a shared host: `HARMONY_ATTEMPT` is in every handler's env) and obtain a receipt outside the real attempt. | root, once **per account** (not per worker id) |
| `machine_id`, `host` | Yes (binding to the physical host and operator policy). | root, once per host |
| `journal` path | No (cross-check only; the authority is the truth). | derivable: `<state-root>/<worker>/active.json`, worker id validated by `name()` |
| `token` (authority credential) | Yes (it speaks for the worker to the authority). | root-held. Today one per worker. |
| `socket` | No. | one per host |
| `class` grants | Yes. | already per **physical host** in `compilation_policy`, not per worker |

So the per-worker unit is an *implementation shape*, not a security boundary, with exactly two real per-worker residues: the account (uid) and the credential.

### Answer

**Yes: one root verifier per host can serve every worker id enrolled to that host without weakening the fence**, under four conditions:

1. **Enrolment is of the account and the host, by root, once.** `/etc/livestack/compilation-host.json` (root-owned, same trust walk as the existing registry): `{host, machine_id, authority, accounts:[{uid, state_root}]}`. Worker ids are *not* enumerated by root. A worker id is served iff the authority vouches for it (below) *and* the peer uid is an enrolled account *and* the peer's cgroup is inside `harmony-work-sha256(request.worker)[:16]-<attempt>.service`.
2. **One host-scoped verifier credential** (authority principal flag `verifier_host: <enrolment>`). `verify-compilation` accepts `{worker, boot, attempt_id, fence, input_digest, class}` from that credential **only** for workers whose principal host is that enrolment, and answers exactly as it does for the worker token. It cannot claim, report, complete, upload or renew. Per-worker-token mode stays supported (migration, mixed hosts). The credential is strictly weaker than the six worker tokens that sit in the same root-only directory today.
3. **All seven kernel/authority checks stay, per request**, with the row looked up by the request's worker id after step 1. The server becomes threaded (per-request monotonic deadline instead of process-wide `SIGALRM`; the Windows path already works this way, bound 16 concurrent) so one slow `systemctl show` cannot block another slot's launch.
4. **No new trust in the consumer**: `launch_contract.verify_launch` already maps any worker id to a socket path; many ids -> one host socket needs no consumer change. Unenrolled worker id or uid -> the same named refusal as today (`compilation_verifier_slot_unavailable`).

**What this does weaken, and the mitigations.** (i) Availability blast radius: one process serves the host. Mitigation: atomic validated reload of the host file on SIGHUP, `Restart=on-failure`, and the roster flags `verifier: unreachable` per worker; per-slot units remain available for hosts that want isolation. (ii) The host credential concentrates authority-side trust: bounded to `verify-compilation` and to one host, and the file already holds six tokens. (iii) Separate-uid hosts keep their per-uid separation because accounts are enrolled individually; a host that mixes trusted and untrusted accounts must enrol only the trusted ones.

**macOS / Windows.** The launchd job label and Windows Job Object name derive from the worker id the same way, so the per-request derivation carries over. The Windows pipe ACL admits one SID today (`PipeListener(pipe, worker_sid)`); the host form takes a list of enrolled SIDs. Both are separate tasks gated on their own hosts (`apple-host-compilation`, `windows-host-worker`).

**Smallest step that needs none of the above.** `livestack_node.workloads.enroll_worker` (root): given a host registry, regenerate the per-worker unit, `verifier.json`, SDK generation and registry fragment for each listed worker, idempotently (it already exists as `zzops/scripts/stage_compilation_verifier.py`, one worker at a time, with the uid read from the worker config's owner). Add: `--all`, a drift report, `--check` (read-only: unit active? socket answers? registry entry present? uid matches?), and a worker-reported `verifier` fact so a forgotten slot is a roster line, not a refused launch at 3 a.m. This is phase 1 and lands value regardless of the per-host server.

## Decisions

### D1. Requirements: three tiers, one effective set

`effective(handler, release) = derived(manifest) AND hints(manifest.requirements) AND profile(operator)`, strictest wins per dimension; **only the profile can set `access`, `compilation_classes`, `attested_labels`, `execution`**.

*Derived (manifest v1, already inside the release digest):* `platform` -> os, `architecture` -> arch, `backend`, `runtime_id`.

*Package hints (new `harmony-handler-package.v2`, `requirements`, closed keys, inside the digest):*

```json
"requirements": {
  "memory_bytes": 9663676416, "disk_bytes": 64424509440, "cpu": 4,
  "tools": ["rootless_docker", "cargo", "flutter", "android_sdk"],
  "selftest": {"arguments": ["--selftest"], "max_seconds": 120}
}
```

`tools` is a **closed vocabulary** of named probes (`rootless_docker`, `cargo`, `flutter`, `android_sdk`, `chrome`, `docker_native_frontend`...), each a bounded local check in `runtime_discovery.py` that returns `{path, version}` or `{reason}`. An unknown tool name makes the release unassignable by name (never silently ignored). `memory_bytes`/`disk_bytes` compare to worker `capacity` (the operator ceiling, which is what admission can ever grant), not to momentary `available`. v1 manifests are still accepted: no hints, derived tier only.

*Operator profile (authority config, extends the existing `handler_release_policy.handlers.<id>` which today may only narrow runtimes/backends/enabled):*

```json
"benchday.compilation.rust-check.v1": {
  "access": "host_enrolled",
  "compilation_classes": ["rust"],
  "requires": {"labels": {"region": ["ca","cn"]}, "memory_bytes": 8589934592},
  "execution": {"max_seconds": 5400, "infrastructure_exit_codes": [75]}
}
```

`access` in {`open` (default for unprofiled handlers), `host_enrolled`, `attested`}. `compilation_classes` replaces the typed `compilation_handlers` (if both are set and disagree, the authority refuses to start/reload by name). `execution` carries what worker configs hold today per handler (`max_seconds`, `max_tasks`, exit codes, `output_max_bytes`); a worker may only tighten. Package metadata still cannot grant: this keeps the existing `workload-handler-releases` rule "package metadata SHALL NOT grant permissions".

### D2. Facts are claims; the authority decides; the worker double-checks

Worker report gains a bounded closed `facts` block (<= 8 KiB): `os`, `arch`, `cpu`, `memory_total_bytes`, `disk_total_bytes`, `runtimes{id:{version,source}}`, `tools{name:{version}|{reason}}`, `verifier{state:enrolled|missing|unreachable|n/a, reason}`, `facts_version`. Facts are produced by one function reusing `runtime_discovery.probe` and the host view the worker already samples; labels stay as they are (placement selectors keep working).

The authority evaluates `eligibility.evaluate(profile, release, facts, principal, policy_snapshot) -> (state, reason)`, a **pure** function (brains stay pure and inference-free, per `openspec/config.yaml`): no I/O, no clock. It is memoised per worker by `hash(facts, policy_revision, registry_generation)`, so a sync costs one in-memory pass: workers x handlers <= 128 x 64 = 8192 evaluations, only on change (bounded fan-out, no per-pair database round trip; the existing `desired()` set query stays one statement).

The response to `worker/report` carries `handler_sync` as today, now with an `assigned` map {handler: digest}. The worker evaluates the same pure function locally on its own facts and **refuses with a named reason any assignment it would not make itself** (defence in depth against an authority bug or a spoofed response); disagreement is a roster line (`authority_assigned_but_worker_refuses: ...`), never silent.

Circularity removed: `desired(report['handlers'])` is replaced by the authority's own assigned set; `report['handlers']` becomes "what I am serving now", not "what I am allowed to receive".

### D3. Access levels and the admin boundary

| Level | Examples | Code distributed automatically? | Served when |
|---|---|---|---|
| `open` | `e2e.*`, `harmony.probe`, `thumbnail`, `title_gen`, `commerce.report` | yes, to every matching worker with assignment mode on | install verified + selftest |
| `host_enrolled` | `compilation.*` (rust, flutter, image, node, native, apple, windows) | yes, to workers on a host that the **operator policy** allows for the handler's classes | additionally `facts.verifier.state == enrolled` **and** authority policy `hosts[physical].classes` covers `compilation_classes` |
| `attested` | `release.app.android`, `release.hub`, any signing handler | the package may be distributed, but the handler is *served* only on a host with an operator attestation | additionally operator `host_attestations[physical]` provides the profile's `attested_labels` (e.g. `android_signer_sha256`) |

Rules that never bend: (1) **keystores, signing keys, deploy credentials are never in a package, a sync or a report**; the handler finds them in the worker's own protected environment, set by an administrator when the host is attested. (2) **Reserved label keys** (`signing`, `android_signer_sha256` and any key a profile's `attested_labels` names) are taken from operator attestation; a worker-reported value is dropped and recorded as `reserved_label_ignored`. Phase-gated: observe-only reports the conflict before enforcing. (3) A worker may opt **out** (`handler_policy.deny`) at any time; opting **in** to a level above `open` is never a worker-side act. (4) The privilege to compile is the root verifier plus operator host classes, exactly as today; assignment adds *code*, not *permission*. Installing `rust-check` on a worker that has no enrolled verifier is harmless: it is not served, and says why.

### D4. Lifecycle, safety and bounds

Per worker x handler: `assigned -> downloading -> installed (existing digest-verified atomic install) -> selftest -> serving`; any failure -> `quarantined(reason)` with the previous digest, if any, still serving (the existing pointer semantics: a failed activation returns without committing).

- **Selftest.** (a) every `tools` probe passes; (b) if `selftest` is declared, it runs as a worker-local supervised zero-input attempt through the handler's own backend and resource limits (the `harmony.probe` pattern; no authority lease), result `{digest, ok, at, reason}` in the report; (c) none declared -> `serving` with the roster note `no selftest`. A privileged handler's launch path cannot be self-tested outside a live attempt (the verifier requires one); its readiness is `facts.verifier` plus an authority-submitted canary of an existing probe-class handler. Stated limit: the first real compilation attempt remains the first proof of the full path.
- **Withdrawal.** When a worker stops matching (memory ceiling lowered, runtime vanished, deny added, profile changed, release retired) the authority stops assigning; the worker drops the handler from `report.handlers` **after** any running attempt for it finishes (never kills one; the journal already protects its digest), records `withdrawn: <reason>`, and leaves the package to the existing retention sweep. Queued pinned jobs keep the existing named release-availability wait.
- **Rollback.** Unchanged and per handler: activate the retained digest. A selftest failure on a new default keeps the previous digest serving on that worker.
- **Flapping.** Assignment changes per worker are limited (<= 8 per hour; further changes are held with reason `assignment_rate_limited`), facts changes shorter than one report interval are debounced. Every change is a bounded event (ring of 256 rows, enforced by the same DELETE-oldest pattern as `handler_release_events`): this is the ledger record for each assignment decision (who, what, why, digests, policy revision, generation).
- **Storage bounds are inherited** (`MAX_INSTALLED_BYTES` 16 GiB, 256 packages, 3 transients). When a mandatory install would exceed them, the state is `ineligible(worker_package_capacity)`, not a silent skip, and capacity-driven eviction stays authority-side only.
- **Absence never looks like failure and failure never looks like absence**: a worker that sends no `facts` (old release) is `facts_unavailable` (explicit list honoured), never "ineligible".

### D5. Roster and diagnostics

`roster.py` already names disagreements; it gains the matrix, one row per worker x registry-or-listed handler, states: `serving`, `eligible_not_installed` (will be offered), `installing`, `selftest_pending`, `quarantined`, `ineligible`, `withheld`, `override_serving` (listed by hand but the evaluator says it should not: observe-mode finding), `facts_unavailable`. `reason` is a code plus a human sentence with the numbers (`memory_below: needs 9.0 GiB, has 6.0 GiB`). Closed reason vocabulary (`os_mismatch`, `arch_mismatch`, `backend_unsupported`, `runtime_missing:<id>`, `tool_missing:<tool> (<probe reason>)`, `memory_below`, `disk_below`, `cpu_below`, `label_missing:<k>`, `host_not_enrolled:<class>`, `verifier_missing`, `verifier_unreachable`, `signer_not_attested`, `denied_by_worker`, `denied_by_operator`, `worker_package_capacity`, `assignment_rate_limited`, `facts_unavailable`, `facts_stale`). `GET handlers/<h>/capacity` (3429815e) additionally returns `eligible_not_installed` and a top-reasons histogram so a caller sizing work learns why capacity is low, not only how much there is. CLI: `workload handlers matrix [--handler H] [--worker W]`.

### D6. Migration of the explicit lists

Precedence: `handler_policy.deny` > explicit `handlers` entry (a pin: served as today, still subject to verified install) > computed assignment. Modes via worker config `handler_assignment`: `off` (default; byte-for-byte today), `observe` (facts reported, assignment computed and published, nothing installed or changed), `open` (assign open-level only), `host_enrolled`, `attested_report_only`. Per-handler execution policy moves into the operator profile (`execution`); a worker's own `handlers[h]` block, when present, overrides only to tighten. Authority `handlers` becomes the union of config and profiled/registry ids (add-only, as now); `compilation_handlers` derives from profile `compilation_classes`. After a soak per level, hand lists are deleted from worker configs; `deny` remains.

Prerequisite outside this repo: handlers that still run from an on-disk bundle (`/home/ubuntu/.local/share/benchday-harmony-handlers/<sha>/...`: flutter-check, image, e2e.dependencies/full on some slots) must be published as v1/v2 packages first. Only registry-managed handlers can be assigned; the rest stay pins and are reported `override_serving` / `not_packaged`.

### D7. Benchday

Benchday's bundle builder (`scripts/build-handler-bundle.mjs`) emits `v2` manifests with a per-handler `requirements` table kept next to the handler source, and an operator-profile fragment it prints for the authority config (not applied by the builder). `docs/harmony-worker-enrolment.md` loses the per-handler worker-config walkthrough; it keeps host enrolment (verifier, attestation) and gains "read the matrix". `docs/e2e-admission-capacity.md` references the roster instead of hand-counted slots. Companion change in the Benchday repo; this change only fixes the contract.

## Risks / Trade-offs

- **Observe-mode is the only proof the evaluator is right.** A wrong requirement silently *removes* capacity (a worker stops serving) -> withdrawal waits for running attempts, rollout is per level on one canary, and `override_serving` shows disagreements before anything is withdrawn. Pins are never withdrawn by computed assignment.
- **Understated package hints** (a package claims 1 GiB, needs 9) -> the operator profile may raise; a job that OOMs is an `infrastructure` outcome already; roster shows per-handler failure rate is out of scope.
- **Facts are self-reported.** Wrong facts can only reduce, not widen, privilege: access levels above `open` read operator policy, not facts (the `verifier` fact is cross-checked by the first real launch).
- **Per-host verifier is a larger root-code change than `enroll-worker`.** Sequenced second; phase 1 stands alone.
- **Shared-uid hosts** (all of zz-joe) cannot separate workers by uid; documented, unchanged from today (see hard question).
- **Reserved-label enforcement could unserve a release job** whose label is only worker-asserted today (`zz-joe-release` `android_signer_sha256`). -> attestation provisioned before enforcement; observe first.
- **Rate limit hides real change** -> `assignment_rate_limited` is itself a named state.

## Migration Plan (rollout, per phase; each is independently revertible by setting the mode back to `off`)

0. **Observe-only.** Ship evaluator, `facts`, matrix, `handler_assignment: observe`. Deploy to the authority and workers through the existing guarded paths; nothing installed or withdrawn. Exit: the diff between today's lists and `would_serve` is explained line by line (intentional exclusions become profile entries; real gaps are listed).
1. **`enroll-worker --all/--check`** and the worker `verifier` fact. Exit: every compilation slot on zz-joe, WSL and Mac reports `enrolled` or a named reason.
2. **Canary `open`** on `zz-joe-e2e-3` (already the runtime-discovery canary): assign `e2e.*`, `harmony.probe`. Exit: identical job outcomes for a week.
3. **`open` fleet-wide**, then `host_enrolled` on canary, then fleet. `compilation.rust-check` appears on e2e-1/-3/-4 and `-wsl-2` gets flutter-check/image if its facts allow (13 GiB; profile floors decide) without a config edit.
4. **Per-host verifier** (condition 1-4 above) on zz-joe first; per-slot units retired after one release cycle.
5. **Attested**: report-only; enforcement of reserved labels last.
6. Hand lists deleted from worker configs; `deny` stays.

Rollback of any phase: set `handler_assignment: off` and SIGHUP/restart that worker; pins keep serving; the authority ignores `facts`.

## Open Questions

1. Should a worker with `handler_assignment: open` install `open` handlers it was never asked to (eager) or on first matching queued job (lazy, saves disk on small hosts)? Default proposed: eager for defaults only, within the existing bounds.
2. May `host_enrolled` auto-assign wait on the owner's per-class approval (e.g. `image` builds can run docker)? Proposed: yes, per class, in the operator profile.
3. Memory floors: operator should choose per handler (`flutter-check` 9 GiB?) from measured peaks; this change supplies the field, not the numbers.
4. Do Windows and macOS workers join the per-host verifier in this change or later? Proposed: later, own tasks.
