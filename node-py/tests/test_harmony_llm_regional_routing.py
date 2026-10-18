"""Regional routing: a view that is TTL'd and a target that validates.

Covers harmony-regional-llm-routing: the routed-request contract (`expect`,
`409 route_stale`, version), the router's one retry, the request id that joins
route and demand records, bounded stale views, region allowances, router mode.
"""
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"
sys.path.insert(0, str(HERE))

import fleet_route  # noqa: E402
from livestack_node.fleet_auth import AuthError, Principal, effective_regions  # noqa: E402
from livestack_node.fleet_rank import rank  # noqa: E402


# ── pure pieces ───────────────────────────────────────────────────────────

def test_the_routed_header_round_trips_and_a_bare_id_is_a_cold_v1_route():
    h = fleet_route.format_routed("lappy", "warm")
    assert h == "lappy;expect=warm;v=1"
    r = fleet_route.parse_routed(h)
    assert (r.router_id, r.expect, r.version) == ("lappy", "warm", 1)
    old = fleet_route.parse_routed("xc-tower-ubuntu")
    assert (old.router_id, old.expect, old.version) == ("xc-tower-ubuntu", "cold", 1)
    assert fleet_route.parse_routed(None) is None
    assert fleet_route.parse_routed("a;expect=lukewarm;v=x").expect == "cold"


def _node(host, region, resident, peer_port=8188):
    return {"peer": f"http://{host}:{peer_port}/livestack", "state": "fresh",
            "ready": resident, "kinds": ["llm"], "region": region,
            "detail": "resident" if resident else "no unit resident",
            "units": [{"kind": "llm_a", "resident": resident,
                       "attributes": {"class": "llm", "params_b": 27}}]}


def _view(*nodes, links=None):
    hosts = {}
    for n in nodes:
        h = n["peer"].split("//")[1].split(":")[0]
        hosts.setdefault(h, {"nodes": [], "links": (links or {}).get(h, {})})["nodes"].append(n)
    return {"generated_at": time.time(), "hosts": hosts}


def test_a_cn_entry_ranks_a_cn_target_first_even_when_the_broker_sits_in_na():
    view = _view(_node("na-gpu", "na", True), _node("cn-gpu", "cn", True),
                 links={"na-gpu": {"cn-gpu": 250.0}, "cn-gpu": {"na-gpu": 250.0}})
    _t, rec = fleet_route.choose(view, {"class": "llm"}, [], "http://cn-router:8188",
                                 vantage="region:cn")
    assert rec["vantage"] == "region:cn"
    assert rec["chosen"] == "http://cn-gpu:8188"
    assert [t["target_id"] for t in rec["targets"]][0] == "http://cn-gpu:8188"
    _t, rec = fleet_route.choose(view, {"class": "llm"}, [], "http://na-router:8188",
                                 vantage="region:na")
    assert rec["chosen"] == "http://na-gpu:8188"


def test_expect_says_whether_the_chosen_target_was_warm_in_the_ranking():
    warm = _view(_node("g", "na", True))
    cold = _view(_node("g", "na", False))
    assert fleet_route.choose(warm, {"class": "llm"}, [], "http://x:1")[1]["expect"] == "warm"
    assert fleet_route.choose(cold, {"class": "llm"}, [], "http://x:1")[1]["expect"] == "cold"


def test_the_record_names_regions_and_filters_a_forbidden_warm_node():
    view = _view(_node("na-gpu", "na", True), _node("cn-gpu", "cn", False))
    tgt, rec = fleet_route.choose(view, {"class": "llm"}, [], "http://x:1", regions=["cn"])
    assert tgt == "http://cn-gpu:8188" and rec["regions"] == ["cn"] and rec["expect"] == "cold"
    assert any("region na" in f["reason"] for f in rec["filtered"])


def test_choose_can_exclude_a_target_that_already_refused():
    view = _view(_node("a", "na", True), _node("b", "na", True))
    first, _ = fleet_route.choose(view, {"class": "llm"}, [], "http://x:1")
    second, _ = fleet_route.choose(view, {"class": "llm"}, [], "http://x:1", exclude=[first])
    assert second and second != first


def test_a_stale_view_stands_in_only_up_to_the_bound(monkeypatch):
    url = "http://fleet.invalid:8801"
    view = _view(_node("g", "na", True))
    fleet_route._cache.clear()
    fleet_route._cache[url] = (time.time() - 60, view)       # older than the TTL, fresh enough to trust
    monkeypatch.setattr(fleet_route.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert fleet_route.fleet_view(url, stale_max_s=300) is view
    assert 59 < fleet_route.view_age_s(url) < 70
    assert fleet_route.fleet_view(url, stale_max_s=30) is None   # beyond the bound: local only
    fleet_route._cache.clear()


def test_mark_cold_drops_a_target_from_the_cached_view_until_refresh():
    url = "http://fleet.invalid:8802"
    fleet_route._cache.clear()
    fleet_route._cache[url] = (time.time(), _view(_node("g", "na", True)))
    fleet_route.mark_cold(url, "http://g:8188")
    assert fleet_route.choose(fleet_route.fleet_view(url), {"class": "llm"}, [],
                              "http://x:1")[1]["expect"] == "cold"
    fleet_route._cache.clear()


def test_wants_fleet_route_names_its_trigger():
    speed = [{"metric": "llm.decode_tok_s", "direction": "max"}]
    assert fleet_route.wants_fleet_route(speed, {}) == "speed"
    assert fleet_route.wants_fleet_route([], {}) is None
    assert fleet_route.wants_fleet_route([], {}, no_warm_local=True) == "no_warm_local"
    assert fleet_route.wants_fleet_route(speed, {fleet_route.ROUTED_HEADER: "x"}) is None


def test_a_principal_allowance_can_be_narrowed_never_widened():
    p = Principal(name="hub", owner="o", regions=frozenset({"na", "cn"}))
    assert effective_regions(p, None) == (["cn", "na"], "principal")
    assert effective_regions(p, ["na"]) == (["na"], "request")
    assert effective_regions(p, "NA, cn") == (["cn", "na"], "request")
    with pytest.raises(AuthError) as e:
        effective_regions(p, ["eu"])
    assert e.value.status == 403 and "eu" in e.value.detail and "cn/na" in e.value.detail
    free = Principal(name="open", owner="o")
    assert effective_regions(free, None) == (None, "none")
    assert effective_regions(free, ["eu"]) == (["eu"], "request")
    assert effective_regions(None, None) == (None, "none")


def test_principal_regions_load_from_the_token_table():
    from livestack_node.fleet_auth import load_principals
    tbl = load_principals(json.dumps({
        "a" * 20: {"name": "hub", "owner": "hub", "regions": ["NA"]},
        "b" * 20: {"name": "bad", "owner": "bad", "regions": []}}))
    assert tbl["a" * 20].regions == frozenset({"na"})
    assert "b" * 20 not in tbl          # an empty list would silently mean "everywhere"


# ── a fake serving node ───────────────────────────────────────────────────

class _Target:
    """A stand-in serving node: answers with `script` per request, records what
    it was sent."""

    def __init__(self, script, on_request=None):
        self.script, self.got = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if on_request:
                    on_request()
                n = int(self.headers.get("content-length") or 0)
                outer.got.append({"body": json.loads(self.rfile.read(n) or b"{}"),
                                  "routed": self.headers.get("x-harmony-routed"),
                                  "rid": self.headers.get("x-request-id"),
                                  "auth": self.headers.get("authorization")})
                step = outer.script.pop(0) if outer.script else ("200", {"ok": 1})
                code, payload = step
                if code == "drop":
                    self.connection.close()
                    return
                out = json.dumps(payload).encode()
                self.send_response(int(code))
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def node(self, region="na", resident=True):
        n = _node("127.0.0.1", region, resident, self.srv.server_address[1])
        return n

    def close(self):
        self.srv.shutdown()


STALE = ("409", {"error": "route_stale", "reason": "not_resident"})


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _send(first, rechoose, attempts, **kw):
    return fleet_route.send_routed(
        first=first, rechoose=rechoose, router_id="r1", request_id="rid-1", method="POST",
        path="chat/completions", body=b'{"model":"require:class=llm"}',
        headers={"authorization": "Bearer caller"}, params={}, timeout=5.0,
        attempts=attempts, **kw)


def test_a_stale_route_is_retried_once_on_the_next_target():
    a, b = _Target([STALE]), _Target([("200", {"ok": 1})])
    try:
        async def go():
            attempts = []
            resp, client = await _send((a.base, "warm"), lambda tried: (b.base, "warm"), attempts)
            body = await resp.aread()
            await resp.aclose()
            await client.aclose()
            return resp.status_code, json.loads(body), attempts
        status, body, attempts = _run(go())
        assert status == 200 and body == {"ok": 1}
        assert [x["result"] for x in attempts] == ["route_stale/not_resident", "ok"]
        # The header the contract defines, the caller's own credentials, one id.
        assert a.got[0]["routed"] == "r1;expect=warm;v=1" == b.got[0]["routed"]
        assert a.got[0]["rid"] == b.got[0]["rid"] == "rid-1"
        assert a.got[0]["auth"] == "Bearer caller"
    finally:
        a.close(), b.close()


def test_two_refusals_end_in_a_failure_that_names_both_attempts():
    a, b = _Target([STALE]), _Target([("429", {"error": "busy"})])
    try:
        async def go():
            attempts = []
            with pytest.raises(fleet_route.RouteFailed) as e:
                await _send((a.base, "warm"), lambda tried: (b.base, "cold"), attempts)
            return e.value
        err = _run(go())
        assert err.status == 429
        assert [x["target"] for x in err.attempts] == [a.base, b.base]
        assert len(a.got) == len(b.got) == 1            # one retry, not a loop
    finally:
        a.close(), b.close()


def test_a_connection_failure_before_the_response_is_retried():
    a, b = _Target([("drop", None)]), _Target([("200", {"ok": 1})])
    try:
        async def go():
            attempts = []
            resp, client = await _send((a.base, "cold"), lambda tried: (b.base, "cold"), attempts)
            await resp.aclose()
            await client.aclose()
            return attempts
        attempts = _run(go())
        assert attempts[0]["result"] and attempts[0]["result"] != "ok" and attempts[1]["result"] == "ok"
    finally:
        a.close(), b.close()


SATURATED = ("409", {"error": "route_stale", "reason": "saturated"})


def test_when_every_target_is_merely_busy_the_last_attempt_queues_cold():
    """Warm-routed requests never queue; if both targets refuse for saturation the
    request is handed cold to the first target (which may queue), not failed."""
    a, b = _Target([SATURATED, ("200", {"ok": 1})]), _Target([SATURATED])
    try:
        async def go():
            attempts = []
            resp, client = await _send((a.base, "warm"), lambda tried: (b.base, "warm"), attempts)
            await resp.aread(); await resp.aclose(); await client.aclose()
            return resp.status_code, attempts
        status, attempts = _run(go())
        assert status == 200
        assert [x["result"] for x in attempts] == ["route_stale/saturated", "route_stale/saturated", "ok"]
        assert attempts[-1]["expect"] == "cold" and attempts[-1]["target"] == a.base
        assert a.got[-1]["routed"].endswith("expect=cold;v=1")
    finally:
        a.close(), b.close()


def test_a_non_saturation_refusal_is_not_retried_cold():
    a, b = _Target([STALE]), _Target([SATURATED])
    try:
        async def go():
            with pytest.raises(fleet_route.RouteFailed) as e:
                await _send((a.base, "warm"), lambda tried: (b.base, "warm"), [])
            return e.value
        assert len(_run(go()).attempts) == 2 and len(a.got) == 1
    finally:
        a.close(), b.close()


def test_nothing_to_retry_onto_is_a_failure_not_a_replay():
    a = _Target([STALE])
    try:
        async def go():
            with pytest.raises(fleet_route.RouteFailed) as e:
                await _send((a.base, "warm"), lambda tried: (None, "cold"), [])
            return e.value
        assert _run(go()).status == 503
        assert len(a.got) == 1
    finally:
        a.close()


def test_not_resident_drops_the_target_from_the_cached_view():
    a, b = _Target([STALE]), _Target([])
    url = "http://fleet.invalid:8803"
    try:
        fleet_route._cache.clear()
        fleet_route._cache[url] = (time.time(), _view(a.node()))
        async def go():
            resp, client = await _send((a.base, "warm"), lambda tried: (b.base, "warm"), [],
                                       on_stale=lambda t: fleet_route.mark_cold(url, t))
            await resp.aclose()
            await client.aclose()
        _run(go())
        assert fleet_route.choose(fleet_route.fleet_view(url), {"class": "llm"}, [],
                                  "http://x:1")[1]["expect"] == "cold"
    finally:
        fleet_route._cache.clear()
        a.close(), b.close()


def test_a_contract_mismatch_409_is_handed_back_not_retried():
    a = _Target([("409", {"error": "route_contract", "router_version": 9, "supported": 1})])
    try:
        async def go():
            attempts = []
            resp, client = await _send((a.base, "warm"), lambda tried: ("http://nope", "warm"), attempts)
            raw = await resp.aread()
            await resp.aclose()
            await client.aclose()
            return resp.status_code, json.loads(raw)
        status, body = _run(go())
        assert status == 409 and body["error"] == "route_contract"
        assert len(a.got) == 1
    finally:
        a.close()


# ── the serving node and the request path ─────────────────────────────────

@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("regional")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/27b", "port": 8189, "footprint_gb": 21,
         "max_model_len": "24576", "default": True,
         "attributes": {"class": "llm", "params_b": 27, "context_len": 24576}},
        {"name": "flash_next", "model": "m/125b", "port": 8191, "footprint_gb": 21,
         "engine": "strata",
         "attributes": {"class": "llm", "params_b": 125, "context_len": 131072}},
    ]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
        "HARMONY_LLM_WARM_ON_START": "0",
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_regional", HERE / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_regional"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _Demand:
    enabled = True
    path = "/dev/null"

    def __init__(self):
        self.rows = []

    def record(self, **f):
        self.rows.append(f)


@pytest.fixture
def node(srv, monkeypatch):
    """The node with nothing resident, no broker, and every way of loading
    recorded: a test asserts a load or an admission did NOT happen."""
    calls = {"admit": 0, "ensure": 0, "health": 0}

    def admit(*a, **k):
        calls["admit"] += 1
        return {"kind": "llm_title", "granted": False, "reason": "test: refused"}

    monkeypatch.setattr(srv, "admit", admit)
    monkeypatch.setattr(srv.manager, "ensure", lambda u: calls.__setitem__("ensure", calls["ensure"] + 1))
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: set()))
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "FLEET_URL", "")
    monkeypatch.setattr(srv, "_request_principal", lambda request: None)
    demand = _Demand()
    monkeypatch.setattr(srv, "DEMAND", demand)
    calls["demand"] = demand.rows
    routes = _Demand()
    monkeypatch.setattr(srv, "ROUTES", routes)
    calls["routes"] = routes.rows
    return calls


def _client(srv):
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _chat(client, headers=None, **extra):
    body = {"model": "require:class=llm,params_b>=20",
            "messages": [{"role": "user", "content": "x"}], **extra}
    return client.post("/v1/chat/completions", json=body, headers=headers or {})


WARM = {"x-harmony-routed": "lappy;expect=warm;v=1"}


def test_a_warm_routed_request_for_a_unit_that_is_gone_is_refused_not_loaded(srv, node):
    with _client(srv) as c:
        r = _chat(c, WARM)
    assert r.status_code == 409
    assert r.json()["error"] == "route_stale" and r.json()["reason"] == "not_resident"
    assert node["admit"] == 0 and node["ensure"] == 0        # nothing admitted, nothing loaded
    row = node["demand"][-1]
    assert row["outcome"] == "route_stale" and row["route_stale"] == "not_resident"
    assert row["routed_by"] == "lappy" and row["request_id"]


def test_a_cold_or_unmarked_request_still_admits_as_before(srv, node):
    with _client(srv) as c:
        r = _chat(c, {"x-harmony-routed": "lappy;expect=cold;v=1"})
        r2 = _chat(c)
    assert r.status_code == r2.status_code == 503
    assert node["admit"] == 2                                  # today's behaviour: ask the broker


def test_a_resident_unit_that_no_longer_meets_the_requirement_is_unsatisfied(srv, node, monkeypatch):
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: {"llm_title"}))
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    with _client(srv) as c:
        r = _chat(c, WARM, model="require:class=llm,params_b>=300")
    assert r.status_code == 409 and r.json()["reason"] == "unsatisfied"
    assert node["admit"] == 0 and node["ensure"] == 0


def test_a_saturated_unit_is_refused_not_queued(srv, node, monkeypatch):
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: {"llm_title"}))
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    monkeypatch.setattr(srv._QUEUES, "try_acquire", lambda *a: None)
    acquired = []
    monkeypatch.setattr(srv._QUEUES, "acquire", lambda *a: acquired.append(a))
    with _client(srv) as c:
        r = _chat(c, WARM)
    assert r.status_code == 409 and r.json()["reason"] == "saturated"
    assert acquired == []                                       # never waits in line


def test_a_warm_routed_request_is_served_by_the_resident_unit(srv, node, monkeypatch):
    t = _Target([("200", {"choices": [{"message": {"content": "hi"}}]})])
    try:
        monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: {"llm_title"}))
        monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
        monkeypatch.setattr(srv, "_base_of", lambda name: t.base)
        with _client(srv) as c:
            r = _chat(c, {**WARM, "x-request-id": "rid-42"})
        assert r.status_code == 200
        assert node["admit"] == 0
        assert t.got[0]["rid"] == "rid-42"
        row = node["demand"][-1]
        assert row["outcome"] == "ok" and row["request_id"] == "rid-42" and row["routed_by"] == "lappy"
        assert row["shortcut"] == "warm-routed"
    finally:
        t.close()


def test_the_unit_is_pinned_against_eviction_while_a_warm_request_holds_it(srv, node, monkeypatch):
    seen = {}
    unit = srv.manager.units["llm_title"]
    t = _Target([("200", {"ok": 1})], on_request=lambda: seen.update(
        busy=unit.busy, in_flight=srv._QUEUES.status("llm_title", 1)["in_flight"]))
    try:
        monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: {"llm_title"}))
        monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
        monkeypatch.setattr(srv, "_base_of", lambda name: t.base)
        with _client(srv) as c:
            assert _chat(c, WARM).status_code == 200
        assert seen == {"busy": True, "in_flight": 1}     # held, and counted, while it ran
        assert srv._QUEUES.status("llm_title", 1)["in_flight"] == 0   # and given back
    finally:
        t.close()


def test_a_router_speaking_a_newer_contract_is_refused_naming_both_versions(srv, node):
    with _client(srv) as c:
        r = _chat(c, {"x-harmony-routed": "lappy;expect=warm;v=9"})
    assert r.status_code == 409
    assert r.json() == {"error": "route_contract", "router_version": 9, "supported": 1}
    assert node["admit"] == 0


def test_a_request_id_is_minted_once_and_kept_when_given(srv, node):
    with _client(srv) as c:
        _chat(c)
        _chat(c, {"x-request-id": "mine"})
    ids = [r["request_id"] for r in node["demand"]]
    assert ids[1] == "mine" and ids[0] and ids[0] != "mine"


# ── routing from a node with nothing warm ─────────────────────────────────

def _fleet_with(target):
    return {"generated_at": time.time(), "hosts": {"far": {"nodes": [target.node("na", True)]}}}


def test_an_indifferent_request_with_nothing_warm_here_goes_to_a_warm_peer(srv, node, monkeypatch):
    t = _Target([("200", {"ok": 1})])
    try:
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: _fleet_with(t))
        with _client(srv) as c:
            r = _chat(c)
        assert r.status_code == 200
        assert node["admit"] == 0 and node["ensure"] == 0        # no local load
        got = t.got[0]
        assert got["routed"].endswith(";expect=warm;v=1")
        assert got["body"]["model"] == "require:class=llm,params_b>=20"   # as asked
        rec = node["routes"][-1]["fleet_route"]
        assert rec["trigger"] == "no_warm_local" and rec["expect"] == "warm"
        assert rec["request_id"] == got["rid"] == node["routes"][-1]["request_id"]
        assert rec["attempts"] == [{"target": t.base, "expect": "warm", "result": "ok"}]
        assert "view_age_s" in rec and "view_stale" in rec
    finally:
        t.close()


def test_a_caller_that_named_a_unit_is_not_routed_to_another_model(srv, node, monkeypatch):
    t = _Target([("200", {"ok": 1})])
    try:
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view",
                            lambda url: (_ for _ in ()).throw(AssertionError("fleet consulted")))
        with _client(srv) as c:
            r = c.post("/v1/chat/completions", json={"model": "llm_title", "messages": [
                {"role": "user", "content": "x"}]})
        assert r.status_code == 503 and node["admit"] == 1     # its own admission, not a detour
        assert t.got == []
    finally:
        t.close()


def test_a_warm_local_unit_still_serves_without_looking_at_the_fleet(srv, node, monkeypatch):
    local = _Target([("200", {"ok": 1})])
    try:
        monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: {"llm_title"}))
        monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
        monkeypatch.setattr(srv, "_base_of", lambda name: local.base)
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view",
                            lambda url: (_ for _ in ()).throw(AssertionError("fleet consulted")))
        with _client(srv) as c:
            assert _chat(c).status_code == 200
    finally:
        local.close()


def test_a_stale_route_is_retried_and_the_record_shows_each_attempt(srv, node, monkeypatch):
    a, b = _Target([STALE]), _Target([("200", {"ok": 1})])
    try:
        # `a` is the quieter node, so it is ranked (and tried) first.
        view = {"generated_at": time.time(), "hosts": {
            "a": {"nodes": [{**a.node("na", True), "load": {"in_flight": 0}}]},
            "b": {"nodes": [{**b.node("na", True), "load": {"in_flight": 9}}]}}}
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: view)
        with _client(srv) as c:
            r = _chat(c)
        assert r.status_code == 200
        attempts = node["routes"][-1]["fleet_route"]["attempts"]
        assert [x["result"] for x in attempts] == ["route_stale/not_resident", "ok"]
        assert len(a.got) == len(b.got) == 1
    finally:
        a.close(), b.close()


def test_when_both_attempts_fail_the_answer_names_both(srv, node, monkeypatch):
    a, b = _Target([STALE]), _Target([STALE])
    try:
        view = {"generated_at": time.time(), "hosts": {
            "a": {"nodes": [a.node("na", True)]}, "b": {"nodes": [b.node("na", True)]}}}
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: view)
        with _client(srv) as c:
            r = _chat(c)
        assert r.status_code == 503
        d = r.json()["detail"]
        assert d["error"] == "route_failed" and len(d["attempts"]) == 2
    finally:
        a.close(), b.close()


def test_without_a_fleet_view_the_node_keeps_its_local_behaviour(srv, node, monkeypatch):
    monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
    monkeypatch.setattr(srv, "_fleet_view", lambda url: None)      # beyond the stale bound
    with _client(srv) as c:
        r = _chat(c)
    assert r.status_code == 503 and node["admit"] == 1              # the local broker path
    assert node["demand"][-1]["fleet_route"]["decision"] == "no fleet view"   # served here, our record


# ── region policy at the facade ───────────────────────────────────────────

def test_a_request_may_not_widen_its_principals_region_allowance(srv, node, monkeypatch):
    monkeypatch.setattr(srv, "_request_principal",
                        lambda request: Principal(name="hub", owner="hub", regions=frozenset({"na"})))
    with _client(srv) as c:
        r = _chat(c, harmony_regions=["cn"])
    assert r.status_code == 403
    assert "cn" in r.text and "na" in r.text and "hub" in r.text


def test_the_allowance_confines_the_ranking_and_the_record_says_where_it_came_from(srv, node, monkeypatch):
    na, cn = _Target([("200", {"ok": 1})]), _Target([("200", {"ok": 1})])
    try:
        view = {"generated_at": time.time(), "hosts": {
            "n": {"nodes": [na.node("na", True)]}, "c": {"nodes": [cn.node("cn", True)]}}}
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: view)
        monkeypatch.setattr(srv, "_request_principal",
                            lambda request: Principal(name="hub", owner="hub", regions=frozenset({"cn"})))
        with _client(srv) as c:
            r = _chat(c)
        assert r.status_code == 200 and len(cn.got) == 1 and na.got == []
        rec = node["routes"][-1]["fleet_route"]
        assert rec["regions"] == ["cn"] and rec["regions_source"] == "principal"
    finally:
        na.close(), cn.close()


def test_no_target_in_the_allowed_regions_is_a_refusal_not_an_out_of_policy_serve(srv, node, monkeypatch):
    na = _Target([])
    try:
        view = {"generated_at": time.time(), "hosts": {"n": {"nodes": [na.node("na", True)]}}}
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: view)
        with _client(srv) as c:
            r = _chat(c, harmony_regions=["cn"])
        assert r.status_code == 503 and "cn" in r.text
        assert node["admit"] == 0 and na.got == []
    finally:
        na.close()


# ── the peer shortcut ─────────────────────────────────────────────────────

def test_the_request_path_never_probes_a_remote_peer(srv, node, monkeypatch):
    probes = []
    monkeypatch.setattr(srv, "_same_kind_peers", lambda: [("far", "http://203.0.113.9:8188")])
    monkeypatch.setattr(srv.httpx, "get", lambda url, **k: probes.append(url) or (_ for _ in ()).throw(OSError()))
    with _client(srv) as c:
        r = _chat(c)
    assert r.status_code == 503
    assert [p for p in probes if "203.0.113.9" in p] == []        # zero /health probes to a remote holder


def test_a_same_host_peer_holding_it_resident_is_forwarded_to_as_asked(srv, node, monkeypatch):
    peer = _Target([("200", {"ok": 1})])
    try:
        monkeypatch.setattr(srv, "_same_kind_peers", lambda: [("gpu1", peer.base)])
        monkeypatch.setattr(srv.httpx, "get", lambda url, **k: type("R", (), {
            "json": lambda self: {"units": {"llm_title": {"resident": True, "loading": False}}}})())
        with _client(srv) as c:
            r = _chat(c)
        assert r.status_code == 200
        assert peer.got[0]["body"]["model"] == "require:class=llm,params_b>=20"   # not rewritten
        assert node["demand"] == []            # the peer's record, not ours
        assert node["admit"] == 0
    finally:
        peer.close()


def test_a_same_host_peer_that_is_only_loading_is_not_a_shortcut(srv, monkeypatch):
    monkeypatch.setattr(srv, "_same_kind_peers", lambda: [("gpu1", "http://127.0.0.1:1")])
    monkeypatch.setattr(srv.httpx, "get", lambda url, **k: type("R", (), {
        "json": lambda self: {"units": {"llm_title": {"resident": False, "loading": True}}}})())
    assert srv._held_on_host("llm_title") is None
    assert srv._held_elsewhere("llm_title") == "http://127.0.0.1:1"   # warm-on-start still counts it


# ── the dry run ───────────────────────────────────────────────────────────

def test_the_dry_run_shows_vantage_regions_and_expectation(srv, node, monkeypatch):
    t = _Target([])
    try:
        monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(srv, "_fleet_view", lambda url: _fleet_with(t))
        with _client(srv) as c:
            r = c.post("/harmony/route", json={"model": "require:class=llm",
                                               "messages": [{"role": "user", "content": "x"}]})
        fleet = r.json()["fleet"]
        assert fleet["trigger"] == "no_warm_local" and fleet["expect"] == "warm"
        assert fleet["chosen"] == t.base and "vantage" in fleet and fleet["regions"] == []
        assert t.got == []                                         # a dry run sends nothing
    finally:
        t.close()


def test_health_reports_the_revision_even_when_the_script_is_a_symlink(srv, tmp_path, monkeypatch):
    # Deployed nodes run `~/harmony-llm/server.py`, a symlink into a livestack
    # worktree; the revision must come from the real checkout, not the link's dir.
    monkeypatch.delenv("HARMONY_LLM_REV", raising=False)
    rev = srv._git_revision()
    assert len(rev) == 40 or rev == "unknown"
    from fastapi.testclient import TestClient
    h = TestClient(srv.app).get("/health").json()
    assert h["revision"] == srv._REVISION and h["contract"] == 1 and "region" in h
