"""Region policy holds on the ADMISSION path, exactly as it does on rank.

`POST /fleet/admit` accepts the same `regions` (+ `allow_unknown_region`)
policy `GET /fleet/rank` accepts, applies it to the fleet view BEFORE
scheduling, and records the rejected rows in the admit ledger record. The
guarantee: an admission NEVER places outside the caller's policy. A caller
moved from ranking-then-going to admitting directly must not silently lose
its region guarantee on the way in (the gap this exists to close — a CN-only
warm fleet must never receive an NA-guaranteed placement).
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.hostd import build_app  # noqa: E402


def _node(peer, host, *, region=None, state="fresh", ready=True,
          kinds=("polytts",), units=None, in_flight=0):
    n = {"peer": peer, "state": state, "ready": ready, "kinds": list(kinds),
         "detail": "resident" if ready else "no unit resident",
         "unseen_seconds": 0.0, "device_id": f"{host}/dev"}
    if region:
        n["region"] = region
    if units is not None:
        n["units"] = units
    if in_flight is not None:
        n["load"] = {"in_flight": in_flight}
    return n


def _view(*nodes):
    hosts = {}
    for n in nodes:
        host = n.pop("_host")
        hosts.setdefault(host, {"nodes": []})["nodes"].append(n)
    return {"generated_at": 1000.0, "hosts": hosts}


CN_WARM = _node("http://100.64.0.3:8100/livestack", "zz-tower0",
                region="cn", units=[{"kind": "polytts", "resident": True}])
NA_COLD = _node("http://100.64.0.18:8100/livestack", "xc-tower-ubuntu",
                region="na", ready=True,
                units=[{"kind": "polytts", "resident": False}])
NA_WARM = _node("http://100.64.0.18:8100/livestack", "xc-tower-ubuntu",
                region="na", units=[{"kind": "polytts", "resident": True}])


class _Broker:
    peers = []
    fleet_principals = None   # auth off: owner from the body
    fleet_policy = None

    def __init__(self, view):
        self._view = view
        self.records = []

    def fleet_view(self):
        return self._view

    def owner_usage(self):
        return {}

    def hosted_checkout(self, *a, **k):
        return None

    def emit_admit(self, result, request, lease_id=None):
        self.records.append({"result": dict(result), "request": dict(request)})


def _client(monkeypatch, view):
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")
    broker = _Broker(view)
    return TestClient(build_app(broker), raise_server_exceptions=False), broker


def test_na_only_admit_with_only_cn_warm_refuses_naming_cn(monkeypatch):
    view = _view(dict(CN_WARM, _host="zz-tower0"))
    client, broker = _client(monkeypatch, view)
    r = client.post("/fleet/admit",
                    json={"kind": "polytts", "regions": "na"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is False
    assert body["target"] is None, "never places in CN"
    # The refusal names the exclusion, the same sentence /fleet/rank uses.
    assert "no polytts target in na" in body["reason"]
    assert "100.64.0.3" in body["reason"]
    assert "region cn" in body["reason"]

    # The rejected rows are recorded in the admit ledger record, the same
    # shape /fleet/rank records region_policy in.
    assert len(broker.records) == 1
    policy = broker.records[0]["request"]["region_policy"]
    assert policy["allow"] == ["na"]
    assert any("100.64.0.3" in row["target_id"] and "region cn" in row["why"]
               for row in policy["rejected"])
    # And the response carries it, so a caller sees the policy was applied.
    assert body["region_policy"]["allow"] == ["na"]


def test_na_only_admit_places_on_a_cold_na_node_rather_than_cn(monkeypatch):
    view = _view(dict(CN_WARM, _host="zz-tower0"),
                 dict(NA_COLD, _host="xc-tower-ubuntu"))
    client, broker = _client(monkeypatch, view)
    r = client.post("/fleet/admit",
                    json={"kind": "polytts", "regions": "na"})
    assert r.status_code == 200
    body = r.json()
    assert body["granted"] is True
    assert body["target"]["host_id"] == "xc-tower-ubuntu", body["reason"]
    assert "100.64.0.18" in (body["target"]["target_id"] or "")
    # The CN exclusion is still recorded beside the grant.
    rejected = body["region_policy"]["rejected"]
    assert any("region cn" in row["why"] for row in rejected)


def test_no_regions_means_no_policy_and_todays_behaviour(monkeypatch):
    view = _view(dict(CN_WARM, _host="zz-tower0"))
    client, broker = _client(monkeypatch, view)
    r = client.post("/fleet/admit", json={"kind": "polytts"})
    assert r.json()["granted"] is True
    assert r.json()["target"]["host_id"] == "zz-tower0"
    assert "region_policy" not in r.json()
    assert broker.records[0]["request"]["region_policy"] is None


def test_allow_unknown_region_keeps_an_unlabelled_node(monkeypatch):
    unlabelled = _node("http://10.0.0.9:8100/livestack", "mystery",
                       units=[{"kind": "polytts", "resident": True}])
    view = _view(dict(unlabelled, _host="mystery"))
    client, _ = _client(monkeypatch, view)
    # Default: unknown region is excluded, not matched.
    refused = client.post("/fleet/admit",
                          json={"kind": "polytts", "regions": "na"})
    assert refused.json()["granted"] is False
    assert "no region declared" in refused.json()["reason"]
    # Explicit opt-in: the caller says unlabelled is acceptable.
    allowed = client.post(
        "/fleet/admit",
        json={"kind": "polytts", "regions": "na", "allow_unknown_region": True})
    assert allowed.json()["granted"] is True
