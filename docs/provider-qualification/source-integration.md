# Qualification-only provider source integration

This is source evidence, not an installed provider or a qualified inference route.
Original provider services and accepted work remain untouched. Joe's unmanaged
legacy model admissions have no proven peak bound; live model effects stay refused.

## Capacity authority

`QualificationReservationStore` uses the existing SQLite transactional operation
store pattern. Exact node/source/config/incarnation and job/input records retain
capacity across request loss and restart. A new controller incarnation holds
claims and advances their fence. Nothing automatically expires capacity. The
private read-only node view cannot adopt the writer or mint a claim.

A configured `HostBroker` overlays reserved and held claims in every normal world
snapshot. Qualification claim creation evaluates its raw snapshot under the same
SQLite writer as normal plan-and-dispatch. No normal caller can concurrently spend
that retained capacity. The absent-configuration path is unchanged.

## Configured request boundary

`QualificationController(configuration_path, broker)` requires a private version 1
configuration containing 1..8 exact `nodes`. Each node has only:

- `binding`: the closed reservation node identity;
- `sourceIdentity`: actual producer identity consumed by the canonical client;
- `unit`: exact `kind` and positive declared `footprint`;
- `ownerSocket`: private Unix owner socket;
- `service`: fixed `polytts-qualification.service` or `polyasr-qualification.service`;
- `unitSha256`: exact root-owned installed unit bytes;
- `clientSettings`: existing source/ABI/config-pinned `QualificationClient` settings.

Private configuration constrains an existing canonical permission; it grants none.
`build_app(broker, qualification=controller)` adds only `/qualification/admit`.
Its body is limited to8192 bytes/20 seconds and only accepts `nodeId`, `jobId`,
`ownerId`, `inputDigest`, `modelKind`. A distinct caller credential is forwarded to
the pinned Node client for fresh canonical `provider.qualify` authorization.
Exact replay rechecks permission and actual source/unit/kernel Unix peer identity.
The resulting receipt explicitly says `resourceLease:false, modelLoaded:false`.

## Node loader boundary

`QualificationNodeGate(manager, client, readonly_reader, binding,
owner_socket=..., unit_sha256=..., capacity_url=...)` checks the actual process boot/start/cgroup,
root unit bytes/incarnation, kernel owner socket peer, and exact static
`providerQualificationNode` constraints in the source-bound private service config.
The fixed loopback capacity client disables ambient proxies and redirects, checks
the closed response, and verifies its actual SQLite claim instead of trusting a
JSON lease verdict. It guards both local
`ensure` and `_load`, including calls through a captured manager instance.

The app must enter `gate.work(job, authorization, fence)` **inside its physically
tracked worker**, with the digest computed from that exact managed request.
The gate verifies the actual retained host claim, fresh canonical input permission,
and a real active node capability-kind lease before the loader runs. A mandatory
source-owned synchronizer runs inside that scope before lease release, including
worker failure. Failed barriers retain an uncertain lease/heartbeat and refuse
new model work. Reconciliation requires the actual current authenticated held
shutdown context; startup and expired contexts cannot release uncertainty. It maintains
that lease until the worker exits; handler disconnection is not worker completion.
No other thread inherits the admitted context. Wrong kinds/devices, unbound budgets,
unrelated resident models, lost leases, stale fences, and held claims refuse.
Job completion releases its kind lease, but does not retire model/host capacity.
Only a registered physical model/cgroup/GPU settlement controller may retire it.

## Exact verification boundaries

- Existing `test_hostbroker.py`: 58/58 pass using the current Python 3.11 environment.
- Capacity/store/configured-broker/controller controls: 20 CPU tests pass in that
  environment using these four modules: `test_qualification_capacity`,
  `test_qualification_reservations`, `test_qualification_broker`, and
  `test_qualification_controller`. Canonical
  grants and systemd peers in controller-positive fixtures are explicitly mocked;
  these fixtures are not real operator registration.
- `test_qualification_node.py`: 7/7 pass using the retained CPython 3.12 framework
  venv with real compiled `ModelManager`, actual SQLite, and real capability lease
  storage; loader, canonical permission and kernel peer are explicit CPU mocks.
- Actual pinned Node client constructor/anonymous/forged/job-control/source/ABI
  negatives: 2/2 pass against the caller-bound bundle fixture.
- Existing hostd TestClient tests: 15 fail before handlers on both exact baseline
  `e483a564` and this source because installed Starlette/httpx reject `app=`.
  No dependency changes or test-gate relaxation were made.
- The retained native framework venv lacks `cryptography`, so a combined native
  suite cannot import hostd; it is not a full hostd package environment.

## Still required

Managed-router/entry wiring is now supplied in source: original input hashes are
computed before preparation, qualification ingress closes legacy effects, and the
entry derives boot/process/unit/owner instance identity before attaching the gate.
Actual imported package/environment/seal and installed registration are still open.
Accepted owner-token status/cancel/result preserve their original checks after expiry.
The configured controller must be installed in the **existing recognized host
broker**, with retained state and owner-qualified ingress/rollback controls.
Private operator registration, exact new source/wheels/seals/unit/config tuples,
dedicated `provider.qualify` grants, and proven bounds for every original contender
remain mandatory. Current free VRAM, source fixtures, or a callback lease are not
proof of those conditions. No service restart or model effect was performed.

## Installed entry constraint mapper

Only `qualificationOnly:true` enables the new mapper. Its source-bound private app
record has closed `providerQualificationNode` fields: `nodeId`, `authority`,
`deviceId`, `unitKind`, `hostId`, `capacityUrl`, `reservationPath`, `ownerSocket`,
`unitSha256`, `authorizationClient`, `nvmlSha256`, `activationStore`. Dynamic boot/PID start ticks, systemd invocation
and actual private owner server instance come from the running unit, not self-
authorizing JSON fields. The private configuration digest is derived from actual
bytes and is never embedded into its own configuration. The absent-mode entry
returns without inspecting qualification settings. Six mapper/environment CPU
controls pass; mapper-positive gates are mocked, not installed authority proof.

## Cold physical-device binding

Before importing the qualified server, the entry invokes the SHA-pinned root
`nvidia-smi` read-only UUID query and requires the declared single CUDA index to
match the actual machine/physical UUID device identity. It passes that explicit
device ID and `device_meter=None` to attach, avoiding Torch device-property
initialization. The activation cache is explicitly separate: private owner-socket
directory / `qualification-activation.json`, never the original default cache.
The optional attach `activation_store` argument preserves old behavior when absent.
This metadata check is not model admission or an available-capacity proof.
Actual read-only tower0 NVML binding passed; its Histo occupancy still disqualifies
it for new model effects. No model, unit or original cache effects were performed.


## Explicit authoritative-broker startup

The source entry now accepts the paired trusted-unit environment values
`LIVESTACK_QUALIFICATION_CONFIG` and `LIVESTACK_QUALIFICATION_RESERVATIONS`.
Both must be canonical absolute paths. The former is the existing private,
closed version1 controller configuration; the latter is its private durable
SQLite reservation path. A missing mate, wrong custody/source/client identity,
invalid registered node or existing different broker store refuses startup.
Every configured source/client is checked before adopting the reservation writer.
Configured retained claims enter broker planning before fleet recovery and
serving. With neither value configured, the original app/startup path is used.

This is configured source startup, not an installed broker rollout or a grant.
Actual canonical permission is rechecked by the controller on admission; the
active bound node-kind lease is still required before model loading. A second
private broker does not replace recognized capacity. Shared8799 rollout still
requires genuine owner policy, accepted-work/lease preservation and rollback.
Twenty CPU startup/controller/store/planning controls and58 original broker
checks pass; authorization and GPU peers in these fixtures are mocked. No shared
broker, node, model or service effect was applied. The sealed546 byte constructors
retain their exact old source identity and are not relabeled with this change.
