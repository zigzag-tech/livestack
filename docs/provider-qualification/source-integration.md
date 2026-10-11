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

`QualificationController(configuration_path, broker)` requires a private version1
configuration containing1..8 exact `nodes`. Each node has only:

- `binding`: the closed reservation node identity;
- `sourceIdentity`: actual producer identity consumed by the canonical client;
- `unit`: exact `kind` and positive declared `footprint`;
- `ownerSocket`: private Unix owner socket;
- `service`: fixed `polytts-qualification.service` or `polyasr-qualification.service`;
- `unitSha256`: exact root-owned installed unit bytes;
- `clientSettings`: existing source/ABI/config-pinned `QualificationClient` settings.

Private configuration constrains an existing canonical permission; it grants none.
`build_app(broker, qualification=controller)` adds only `/qualification/admit`.
Its body is limited to8192bytes/20seconds and only accepts `nodeId`, `jobId`,
`ownerId`, `inputDigest`, `modelKind`. A distinct caller credential is forwarded to
the pinned Node client for fresh canonical `provider.qualify` authorization.
Exact replay rechecks permission and actual source/unit/kernel Unix peer identity.
The resulting receipt explicitly says `resourceLease:false, modelLoaded:false`.

## Node loader boundary

`QualificationNodeGate(manager, client, readonly_reader, binding,
owner_socket=..., unit_sha256=...)` checks the actual process boot/start/cgroup,
root unit bytes/incarnation, and kernel owner socket peer. It guards both local
`ensure` and `_load`, including calls through a captured manager instance.

The app must enter `gate.work(job, authorization, fence)` **inside its physically
tracked worker**, with the digest computed from that exact managed request.
The gate verifies the actual retained host claim, fresh canonical input permission,
and a real active node capability-kind lease before the loader runs. It maintains
that lease until the worker exits; handler disconnection is not worker completion.
No other thread inherits the admitted context. Wrong kinds/devices, unbound budgets,
unrelated resident models, lost leases, stale fences, and held claims refuse.
Job completion releases its kind lease, but does not retire model/host capacity.
Only a registered physical model/cgroup/GPU settlement controller may retire it.

## Exact verification boundaries

- Existing `test_hostbroker.py`:58/58 pass using the current Python3.11 environment.
- Capacity/store/configured-broker/controller controls:20 CPU tests pass in that
  environment before importing the new compiled-manager test module. Canonical
  grants and systemd peers in controller-positive fixtures are explicitly mocked;
  these fixtures are not real operator registration.
- `test_qualification_node.py`:5/5 pass using the retained CPython3.12 framework
  venv with real compiled `ModelManager`, actual SQLite, and real capability lease
  storage; loader, canonical permission and kernel peer are explicit CPU mocks.
- Actual pinned Node client constructor/anonymous/forged/job-control/source/ABI
  negatives:2/2 pass against the caller-bound bundle fixture.
- Existing hostd TestClient tests:15 fail before handlers on both exact baseline
  `e483a564` and this source because installed Starlette/httpx reject `app=`.
  No dependency changes or test-gate relaxation were made.
- The retained native framework venv lacks `cryptography`, so a combined native
  suite cannot import hostd; it is not a full hostd package environment.

## Still required

Actual managed-router/bootstrap wiring must compute exact immutable request hashes,
close legacy inference/model-control/WS surfaces, attach the gate before any loader,
and preserve accepted owner-token status/cancel/result after grant expiry.
The configured controller must be installed in the **existing recognized host
broker**, with retained state and owner-qualified ingress/rollback controls.
Private operator registration, exact new source/wheels/seals/unit/config tuples,
dedicated `provider.qualify` grants, and proven bounds for every original contender
remain mandatory. Current free VRAM, source fixtures, or a callback lease are not
proof of those conditions. No service restart or model effect was performed.
