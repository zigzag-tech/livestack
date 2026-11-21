"""fleet_mcp: the add-a-model runbook, vocabulary and fit/match checks the broker serves."""
import pytest

from livestack_node import fleet_mcp as m

GIB = 1024 ** 3


def _node(peer, device, units, cap=24, free=2, state="fresh"):
    return {"peer": peer, "state": state, "device_id": device,
            "device_mem": {"capacity": cap * GIB, "free": free * GIB}, "units": units}


def _unit(kind, vram_gb, attrs, resident=False, residency=2):
    return {"kind": kind, "footprint": {"vram_bytes": vram_gb * GIB}, "attributes": attrs,
            "resident": resident, "residency": residency}


VIEW = {"hosts": {
    "whitby": {"nodes": [_node("http://w:8188/livestack", "whitby/gpu0", [
        _unit("flash_next", 21, {"class": "llm", "params_b": 125, "thinking": True}, resident=True)])]},
    "leaky": {"nodes": [_node("http://l:8188/livestack", "leaky/gpu0", [
        _unit("flash_next", 19, {"class": "llm", "params_b": 125})], cap=32, free=31)]},
    "tower": {"nodes": [
        _node("http://t:8201/livestack", "tower/gpu1", [
            _unit("ocr2", 6, {"class": "llm", "params_b": 3, "ocr": True})], free=10),
        _node("http://t:8766/livestack", "tower/gpu0", [
            _unit("asr", 8, {"class": "asr"}, resident=True, residency=0)], cap=24, free=6),
        _node("http://gone:1/livestack", "tower/gpu2", [_unit("ghost", 1, {"class": "x"})], state="mia")]},
}}


def test_vocabulary_lists_classes_attributes_and_skips_unreachable_nodes():
    v = m.vocabulary(VIEW)
    assert v["classes"] == ["asr", "llm"]              # `x` is on a mia node: not listed
    assert "params_b" in v["attributes"] and 125 in v["attributes"]["params_b"]["values"]
    assert v["kinds"]["flash_next"] == ["leaky", "whitby"]
    assert v["devices"]["whitby/gpu0"]["units"][0]["kind"] == "flash_next"


def test_plan_requires_kind_host_footprint_and_class():
    r = m.plan_unit(VIEW, {"attributes": {}})
    assert not r["ok"]
    text = " ".join(r["errors"])
    assert "`kind`" in text and "`host`" in text and "footprint" in text and "attributes.class" in text


def test_plan_flags_collision_and_misspelt_attribute():
    r = m.plan_unit(VIEW, {"kind": "flash_next", "host": "whitby", "footprint": {"vram_gb": 1},
                           "attributes": {"class": "llm", "param_b": 7}})
    assert any("already declared on whitby" in e for e in r["errors"])
    assert any("`param_b`" in w and "`params_b`" in w for w in r["warnings"])


def test_plan_fit_verdicts_and_only_resident_copy_warning():
    # whitby: 2 GiB free, the only RESIDENT flash_next lives there.
    r = m.plan_unit(VIEW, {"kind": "sr", "host": "whitby", "footprint": {"vram_gb": 12},
                           "attributes": {"class": "imagegen", "task": "upscale"}})
    assert r["ok"] and r["fit"][0]["verdict"] == "fits_after_eviction"
    assert any("ONLY resident `flash_next`" in w for w in r["warnings"])
    assert any("router does not forward" in w for w in r["warnings"])     # new, unrouted class
    # leaky: 31 GiB free -> fits now; too big -> never fits, which is an error.
    assert m.plan_unit(VIEW, {"kind": "sr", "host": "leaky", "footprint": {"vram_gb": 12},
                              "attributes": {"class": "imagegen"}})["fit"][0]["verdict"] == "fits_now"
    big = m.plan_unit(VIEW, {"kind": "sr", "host": "leaky", "footprint": {"vram_gb": 40},
                             "attributes": {"class": "imagegen"}})
    assert big["fit"][0]["verdict"] == "never_fits" and not big["ok"]


def test_plan_hard_pin_blocks_and_exclusive_note():
    r = m.plan_unit(VIEW, {"kind": "big", "device_id": "tower/gpu0", "footprint": {"vram_gb": 20},
                           "attributes": {"class": "imagegen"}, "exclusive_device": True})
    row = r["fit"][0]
    assert row["verdict"] == "blocked_by_pins"
    assert "asr" in row["exclusive_note"]


def test_plan_warns_when_generic_demand_would_reach_the_new_unit():
    r = m.plan_unit(VIEW, {"kind": "tiny", "host": "leaky", "footprint": {"vram_gb": 4},
                           "attributes": {"class": "llm", "params_b": 4}})
    assert r["callers"]["selects_new_unit"] and r["callers"]["also_matches"]
    assert any("generic demand `require:class=llm`" in w for w in r["warnings"])


def test_match_interval_specialist_and_inexpressible():
    r = m.match(VIEW, "require:class=llm,params_b=[100,]")
    assert sorted(h["unit"] for h in r["matches"]) == ["leaky/flash_next", "whitby/flash_next"]
    r = m.match(VIEW, "class=llm,params_b=(,10]")
    assert r["matches"] == []                            # ocr2 is specialist-only
    assert any("specialist-only" in x["why"] for x in r["non_matches"])
    assert [h["unit"] for h in m.match(VIEW, {"class": "llm", "ocr": True})["matches"]] == ["tower/ocr2"]
    r = m.match(VIEW, "require:class=llm,param_b=7")
    assert r["inexpressible"]["attributes"] == ["param_b"]
    assert "params_b" in r["inexpressible"]["close_known"]["param_b"]
    with pytest.raises(ValueError):
        m.match(VIEW, "require:params_b~7")


def test_merged_view_adds_other_brokers_nodes_and_reports_sources():
    mv = m.MergedView(lambda: VIEW, [])
    mv.remote["http://na/fleet"] = {"hosts": {
        "whitby": {"nodes": [VIEW["hosts"]["whitby"]["nodes"][0]]},         # duplicate peer: not added twice
        "joe": {"nodes": [_node("http://j:8210/livestack", "joe/gpu0",
                                [_unit("z_image", 9, {"class": "imagegen"})])]}}}
    mv.status["http://na/fleet"] = "ok"
    v = mv()
    assert len(v["hosts"]["whitby"]["nodes"]) == 1
    assert "imagegen" in m.vocabulary(v)["classes"]
    assert v["sources"]["http://na/fleet"] == "ok"


def test_runbook_ships_with_the_package():
    assert "add-model" in m.runbooks()
    assert "Pick the route" in m.runbook_text("add-model")


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    app = fastapi.FastAPI()
    m.mount(app, lambda: VIEW)
    return TestClient(app)


def _rpc(client, method, params=None, mid=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                    headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_handshake_tools_resources_prompts(client):
    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "t", "version": "0"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert set(init["result"]["capabilities"]) == {"tools", "resources", "prompts"}
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202
    names = [t["name"] for t in _rpc(client, "tools/list")["result"]["tools"]]
    assert names == ["harmony_runbook", "harmony_vocabulary", "harmony_plan_unit", "harmony_match_need"]
    out = _rpc(client, "tools/call", {"name": "harmony_runbook", "arguments": {}})["result"]
    assert "Pick the route" in out["content"][0]["text"]
    out = _rpc(client, "tools/call", {"name": "harmony_match_need", "arguments": {"need": "params_b~1"}})
    assert out["result"]["isError"]
    res = _rpc(client, "resources/read", {"uri": "harmony://runbooks/add-model"})
    assert "Prove it" in res["result"]["contents"][0]["text"]
    p = _rpc(client, "prompts/get", {"name": "add-model", "arguments": {"what": "SeedVR2"}})
    assert "SeedVR2" in p["result"]["messages"][0]["content"]["text"]
    assert _rpc(client, "nope")["error"]["code"] == -32601
    assert client.get("/mcp").status_code == 405


def test_plain_http_mirrors(client):
    assert client.get("/fleet/units/vocabulary").json()["classes"] == ["asr", "llm"]
    assert client.post("/fleet/units/plan", json={"attributes": {}}).json()["ok"] is False
    assert client.post("/fleet/units/match", json={"need": "class=asr"}).json()["matches"][0]["unit"] == "tower/asr"
    assert client.post("/fleet/units/match", json={"need": "x~1"}).status_code == 400
    assert "Pick the route" in client.get("/runbooks/add-model").text
    assert client.get("/runbooks/nope").status_code == 404
