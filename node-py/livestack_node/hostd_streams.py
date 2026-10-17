"""hostd / workload-authority wiring for the native stream producers. OFF unless HARMONY_STREAMS=1.

    HARMONY_STREAMS=1           enable (anything else, or unset: this module does nothing at all)
    HARMONY_STREAMS_SOCKET      override the daemon's ingress socket (default: the SDK's own lookup,
                                $XDG_STATE_HOME/benchday-daemon/streams-ingress.sock)
    HARMONY_STREAMS_DIR         where the bounded state files live (default ~/.cache/livestack/streams-<host>)
    HARMONY_UNITS_GLOBS         unit declaration globs (os.pathsep-separated; default /etc/harmony/*units*.json)

What each process publishes (see openspec services-own-their-streams):

* every hostd:            harmony.host/1 (unit-declaration digest), harmony.request/1 (host scope)
* the fleet broker only   harmony.fleet/1                       (LIVESTACK_DISPATCH=observe)
* the workload authority  harmony.jobs/1 and harmony.workload/1  (livestack_node.workloads.service)

The planner is the existing one: candidates are chosen by `planner._unit_satisfies` and the
`candidate_kinds` ordering key; this module adds no decision logic.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Sequence

from .streams import fleet_facts
from .streams.host_facts import HostFacts, UnitDeclarations, globs_from_env
from .streams.request_authority import RequestAuthority, request_ledger
from .streams.runtime import MAX_JOB_SUBJECTS, StreamsRuntime
from .streams.identity_facts import FLEET_IDENTITY_FACTS, WORKLOAD_IDENTITY_FACTS
from .streams.workload_authority import IntentLedger, StoreBackend, WorkloadAuthority


def enabled(env: Optional[dict] = None) -> bool:
    return (os.environ if env is None else env).get("HARMONY_STREAMS", "").strip() == "1"


def _state_dir(host: str) -> str:
    return os.environ.get("HARMONY_STREAMS_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "livestack",
                                                                 f"streams-{host}")


def planner_candidates(units: Sequence[dict], requires: dict) -> List[dict]:
    """The existing planner's selection: `_unit_satisfies`, the specialist-only guard, and the
    `candidate_kinds` ordering (resident first, smaller footprint, cheaper reload, name)."""
    from . import planner
    fits = []
    for unit in units:
        probe = SimpleNamespace(attributes=unit.get("attributes") or {})
        if planner._unit_satisfies(probe, requires) and not planner._specialist_only(probe, requires):
            fits.append(unit)
    return sorted(fits, key=lambda u: (not u.get("resident"), planner._magnitude(u.get("footprint_map") or {}),
                                       u.get("reload_cost", 1.0), u.get("name", "")))


def broker_units(broker: Any) -> Callable[[str, Optional[str]], List[dict]]:
    """`scope_units(scope, host)` over the broker's own fleet view (the units each node declares)."""
    def scope_units(scope: str, host: Optional[str]) -> List[dict]:
        view = broker.fleet_view()
        out: List[dict] = []
        for host_id, row in (view.get("hosts") or {}).items():
            if scope == "host" and host_id != (host or broker.host_id):
                continue
            for node in row.get("nodes") or []:
                for unit in node.get("units") or []:
                    footprint = dict(unit.get("footprint") or {})
                    out.append({"name": unit.get("kind", ""), "host": host_id, "attributes": dict(unit.get("attributes") or {}),
                                "resident": bool(unit.get("resident")), "footprint_map": footprint,
                                "footprint": sum(v for v in footprint.values() if isinstance(v, (int, float)))})
        return out
    return scope_units


def _run_in_thread(runtime: StreamsRuntime, log: Callable[[str], None]) -> None:
    def main() -> None:
        loop = asyncio.new_event_loop()
        runtime._loop = loop
        asyncio.set_event_loop(loop)
        try:
            runtime.start()
            loop.run_forever()
        except Exception as error:  # noqa: BLE001
            log(f"streams: runtime thread ended: {error!r}")
        finally:
            try:
                loop.run_until_complete(runtime.stop())
            except Exception as error:  # noqa: BLE001
                log(f"streams: shutdown failed: {error!r}")
            finally:
                loop.close()
    thread = threading.Thread(target=main, name="harmony-streams", daemon=True)
    runtime._thread = thread
    thread.start()


def stop_streams_runtime(runtime: Optional[StreamsRuntime], timeout_s: float = 10.0) -> None:
    """Stop a producer loop on its owner thread and wait for its async cleanup."""
    if runtime is None:
        return
    loop = getattr(runtime, "_loop", None)
    thread = getattr(runtime, "_thread", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout_s)
        if thread.is_alive():
            runtime.log("streams: shutdown timed out while stopping producer thread")


def start_for_hostd(broker: Any, host_id: str, observe: bool, log: Callable[[str], None],
                    identity_publisher: Any = None) -> Optional[StreamsRuntime]:
    """Called from hostd.main() just before the server starts. Returns None when the switch is off."""
    if not enabled():
        return None
    state = _state_dir(host_id)
    runtime = StreamsRuntime(os.environ.get("HARMONY_STREAMS_SOCKET") or None, log, host_id)
    facts = HostFacts(host_id, UnitDeclarations(globs_from_env()), os.path.join(state, "host_facts.json"),
                      runtime.host_publisher(), log=log)

    def fleet_reader() -> tuple:
        fleet = broker.fleet_view()
        demand = getattr(broker, "fleet_demand", None)
        if demand is not None:
            fleet["demand"] = demand.snapshot()
        runtime_policy = getattr(broker, "policy_runtime", None)
        if runtime_policy is not None:
            fleet["degraded"] = list(runtime_policy.status().get("degraded") or [])
        return fleet, {"host_id": broker.host_id, "membership": broker.membership_snapshot(), "peers": []}

    request = RequestAuthority(request_ledger(os.path.join(state, "requests.sqlite")), host_id, broker_units(broker),
                               runtime.intent_publisher("harmony.request/1"), f"harmony-{'fleet' if observe else 'host'}:{host_id}",
                               candidates=planner_candidates, log=log)
    identity_reader = None
    if observe:
        from .identity_facts import IdentitySnapshotPublisher, fleet_identity_cut, valid_identity
        raw_authority_id = os.environ.get("LIVESTACK_IDENTITY_AUTHORITY_ID", "").strip()
        if identity_publisher is None and valid_identity(raw_authority_id, max_bytes=220):
            identity_publisher = IdentitySnapshotPublisher("livestack", raw_authority_id)
        if identity_publisher is not None:
            identity_reader = lambda: fleet_identity_cut(broker, identity_publisher)
        else:
            outcome = ("identity_authority_id_invalid" if raw_authority_id
                       else "identity_authority_not_configured")
            log(f"streams: {outcome}; fleet identity contract unavailable")
    runtime.attach(
        host_facts=facts,
        fleet_reader=fleet_reader if observe else None,
        identity_reader=identity_reader,
        identity_contract=FLEET_IDENTITY_FACTS if identity_reader is not None else None,
        identity_authority_id=(identity_publisher.authority_id if identity_reader is not None else None),
        identity_namespace="harmony:node" if identity_reader is not None else None,
        request=request,
    )
    _run_in_thread(runtime, log)
    log(f"streams: HARMONY_STREAMS=1; {'fleet broker' if observe else 'host broker'} producers starting (state {state})")
    return runtime


def _workload_spec_resolver(blobs: Any) -> Optional[Callable[[str, dict], dict]]:
    """Resolve a workload request from the authority's owner-scoped CAS, with a strict byte ceiling."""
    if blobs is None:
        return None

    max_spec_bytes = 1024 * 1024

    def resolve(owner: str, reference: dict) -> dict:
        from .workloads.model import WorkloadError
        media = reference.get("media")
        if media != "application/json":
            raise WorkloadError("workload spec reference media must be application/json", 415)
        digest = reference["digest"][len("sha256:"):]
        if reference["bytes"] > max_spec_bytes:
            raise WorkloadError(f"workload spec exceeds {max_spec_bytes} bytes", 413)
        with blobs.open(owner, digest) as (source, size):
            if size != reference["bytes"]:
                raise WorkloadError("workload spec reference size differs from the stored object", 409)
            raw = source.read(max_spec_bytes + 1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise WorkloadError("workload spec reference digest verification failed", 409)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkloadError("workload spec must be UTF-8 JSON", 400) from error
        if not isinstance(value, dict):
            raise WorkloadError("workload spec must decode to an object", 400)
        return value

    return resolve


def start_for_workload_authority(store: Any, host_id: str, state_dir: str,
                                 resolve_spec: Optional[Callable[[str, dict], dict]] = None,
                                 log: Callable[[str], None] = print, blobs: Any = None,
                                 public_base_url: Optional[str] = None,
                                 identity_reader: Optional[Callable[[], dict]] = None,
                                 identity_authority_id: Optional[str] = None) -> Optional[StreamsRuntime]:
    """Called from workloads.service.main() after the store is recovered. None when the switch is off.

    `resolve_spec` fetches and digest-verifies a by-reference spec into a store submission request. When no
    custom resolver is supplied, the owner's verified JSON object is resolved from `blobs`. A missing
    resolver or blob store refuses submissions by name; input objects are never admitted unchecked."""
    if not enabled():
        return None
    runtime = StreamsRuntime(os.environ.get("HARMONY_STREAMS_SOCKET") or None, log, host_id)
    authority = WorkloadAuthority(IntentLedger(os.path.join(state_dir, "streams", "intents.sqlite")),
                                  StoreBackend(store, resolve_spec or _workload_spec_resolver(blobs), public_base_url,
                                              blobs),
                                  runtime.intent_publisher("harmony.workload/1"),
                                  f"harmony-workload:{host_id}", log=log)

    def jobs_reader() -> List[dict]:
        return authority.backend.recent_jobs(MAX_JOB_SUBJECTS)

    from .identity_facts import valid_identity
    if identity_reader is None or not valid_identity(identity_authority_id, max_bytes=220):
        outcome = ("identity_authority_id_invalid" if identity_authority_id
                   else "identity_authority_not_configured")
        log(f"streams: {outcome}; workload identity contract unavailable")
        identity_reader = None
        identity_authority_id = None
    runtime.attach(
        jobs_reader=jobs_reader,
        identity_reader=identity_reader,
        identity_contract=WORKLOAD_IDENTITY_FACTS if identity_reader is not None else None,
        identity_authority_id=identity_authority_id,
        identity_namespace="livestack:worker" if identity_reader is not None else None,
        workload=authority,
    )
    _run_in_thread(runtime, log)
    log("streams: HARMONY_STREAMS=1; workload authority producers starting")
    return runtime
