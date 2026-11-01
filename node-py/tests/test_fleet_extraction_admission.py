"""Real HTTP wire controls; no residency/model qualification is claimed.

The serving broker itself needs the native shared_py core, unavailable in this
lightweight environment. These controls prove strict HTTP receipts and cleanup
against the current fleet API shape without constructing a fake planner.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.extraction_contract import PROFILE_ID
from livestack_node.decisions.fleet_extraction_admission import FleetExtractionAdmission
from livestack_node.decisions.nominal_27b_adapter import QualifiedEngine


@pytest.fixture
def fleet_http():
    state = {"calls": [], "release_ok": True, "status": 200, "raw": None}
    engine = QualifiedEngine("llm27", "http://engine/v1/chat/completions", "device-test", "cuda",
                             "fixture-revision", "fixture-tokenizer", "fixture-quality", "bf16", 27)
    state["grant"] = {"granted": True, "kind": engine.unit, "lease_id": "lease-test",
                      "target": {"target_id": "http://qualified-node", "host_id": "host-test",
                                 "device_id": engine.device_id}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["calls"].append((self.path, body, self.headers.get("x-harmony-realm"),
                                    self.headers.get("authorization") == "Bearer fixture-token"))
            self.send_response(state["status"] if self.path == "/fleet/admit" else 200)
            self.end_headers()
            result = state["grant"] if self.path == "/fleet/admit" else {"ok": state["release_ok"]}
            self.wfile.write(state["raw"] if state["raw"] is not None else json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield engine, f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


def run(fleet, *, finish=False):
    engine, url, _ = fleet

    async def scenario():
        async with httpx.AsyncClient() as client:
            broker = FleetExtractionAdmission(client=client, broker_url=url, token="fixture-token",
                engine=engine, target_id="http://qualified-node", host_id="host-test")
            grant = await broker.admit(owner="acct_test", realm="realm_test", profile_id=PROFILE_ID,
                request_id="ask_test", deadline_at_ms=int(time.time() * 1000) + 8000)
            if finish:
                await broker.finish(grant)
            return grant
    return asyncio.run(scenario())


def test_real_fleet_grant_and_release_bind_qualified_target_and_authenticated_scope(fleet_http):
    engine, _, state = fleet_http
    grant = run(fleet_http, finish=True)
    assert grant.kind == engine.unit and grant.device_id == engine.device_id
    assert grant.lease_id == "lease-test"
    path, body, realm, authenticated = state["calls"][0]
    assert path == "/fleet/admit" and authenticated and realm == "realm_test"
    assert body["owner"] == "acct_test" and body["kind"] == engine.unit
    assert body["selector"] == {"host_id": "host-test", "device_id": engine.device_id}
    assert 0 < body["estimate"]["duration_s"] <= 8
    assert state["calls"][1][0] == "/lease/lease-test/release"


@pytest.mark.parametrize("fault,cause", [("no_lease", "broker_unavailable"),
    ("wrong_target", "profile_unavailable"), ("degraded", "profile_unavailable"),
    ("capacity", "capacity"), ("outage", "broker_unavailable"),
    ("malformed", "broker_unavailable"), ("overflow", "broker_unavailable")])
def test_strict_fleet_receipts_never_degrade_to_grant(fleet_http, fault, cause):
    _, _, state = fleet_http
    if fault == "no_lease":
        state["grant"]["lease_id"] = None
    elif fault == "wrong_target":
        state["grant"]["target"]["device_id"] = "unqualified-device"
    elif fault == "degraded":
        state["grant"]["degraded"] = "broker down"
    elif fault == "capacity":
        state["status"] = 429
    elif fault == "outage":
        state["status"] = 503
    elif fault == "malformed":
        state["raw"] = b"not JSON"
    else:
        state["raw"] = b" " * 65537
    with pytest.raises(ContractError) as error:
        run(fleet_http)
    assert error.value.cause == cause
    assert len(state["calls"]) == (2 if fault in ("wrong_target", "degraded") else 1)


def test_release_without_acknowledgement_is_not_success(fleet_http):
    _, _, state = fleet_http
    state["release_ok"] = False
    with pytest.raises(ContractError) as error:
        run(fleet_http, finish=True)
    assert error.value.cause == "broker_unavailable"
    assert state["calls"][-1][0] == "/lease/lease-test/release"
