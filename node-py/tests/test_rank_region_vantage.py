"""Ranking from a declared origin.

A caller with no links row of its own names WHERE it is asking from, and the
answer is measured from THERE, not from wherever the fleet broker happens to
sit:

* `vantage=region:<r>` — the median of the measured link rows of the hosts
  in region `r` to each node. The response carries `vantage_used`, the
  receipt that the ranking was re-based and not silently answered from the
  broker's own vantage.
* `vantage=relay:<id>` — a relay the operator declared in LIVESTACK_RELAYS
  (a vantage point the fleet cannot measure itself). Its links land in the
  fleet view as `relays`, and a rank from it returns real bands, not
  `unknown`.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.fleet_rank import rank  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402


def _node(peer, host, *, region=None, kinds=("llm",)):
    n = {"peer": peer, "state": "fresh", "ready": True, "kinds": list(kinds),
         "detail": "resident", "unseen_seconds": 0.0,
         "load": {"in_flight": 0}, "device_id": f"{host}/dev"}
    if region:
        n["region"] = region
    return n


# Two NA hosts measured the CN node at very different times; the broker's own
# probe says something else again. A `region:na` asker must see the NA median
# (200 ms -> band <600), not the broker's 5 ms, and not the outlier 40 ms.
VIEW = {
    "generated_at": 1000.0,
    "hosts": {
        "na-a": {"region": None, "links": {"zz-tower0": 180.0},
                 "nodes": [_node("http://na-a:8100/livestack", "na-a",
                                 region="na")]},
        "na-b": {"region": None, "links": {"zz-tower0": 220.0},
                 "nodes": [_node("http://na-b:8100/livestack", "na-b",
                                 region="na")]},
        "zz-tower0": {"links": {},     # the broker's own row: 5 ms, ignored
                     "nodes": [_node("http://100.64.0.3:8100/livestack",
                                     "zz-tower0", region="cn",
                                     kinds=("llm",))]},
    },
}
VIEW["hosts"]["zz-tower0"]["nodes"][0]["probe_ms"] = 5.0


def test_region_vantage_uses_the_regions_own_measured_links():
    r = rank(VIEW, "llm", vantage="region:na", now=1000.0)
    assert r["vantage_used"] == "region:na"
    cn = next(t for t in r["targets"]
              if t["target_id"] == "http://100.64.0.3:8100")
    # median([180, 220]) == 200 -> band <600. The broker's own 5 ms (<50) and
    # the 40 ms outlier must neither answer.
    assert cn["distance_ms"] == 200.0
    assert cn["distance_band"] == "<600"


def test_region_vantage_with_no_measured_members_has_no_opinion():
    r = rank(VIEW, "llm", vantage="region:eu", now=1000.0)
    assert r["vantage_used"] == "region:eu"
    cn = r["targets"][0]
    assert cn["distance_band"] == "unknown", "no EU links row: no opinion, never a guess"


# -- G.2: relays ----------------------------------------------------------------

RELAYS = {"na-public-la": {"region": "na",
                           "links": {"zz-tower0": 61.0, "na-a": 3.0}}}


def test_relay_vantage_returns_real_bands():
    view = {**VIEW, "relays": RELAYS}
    r = rank(view, "llm", vantage="relay:na-public-la", now=1000.0)
    assert r["vantage_used"] == "relay:na-public-la"
    cn = next(t for t in r["targets"]
              if t["target_id"] == "http://100.64.0.3:8100")
    assert cn["distance_ms"] == 61.0
    assert cn["distance_band"] == "<200", "non-unknown, from the relay's own row"


def test_fleet_view_carries_the_declared_relays(monkeypatch):
    from livestack_node.hostbroker import HostBroker, relays_from_env

    monkeypatch.setenv("LIVESTACK_RELAYS", '{"na-public-la": {"region": "na", '
                       '"links": {"zz-tower0": 61.0}}}')
    assert relays_from_env()["na-public-la"]["links"] == {"zz-tower0": 61.0}
    broker = HostBroker(devices=[], peers=[], clock=lambda: 1000.0)
    view = broker.fleet_view()
    assert view["relays"]["na-public-la"]["region"] == "na"
    assert view["relays"]["na-public-la"]["links"]["zz-tower0"] == 61.0

    # Unset: the key stays absent, exactly like a fleet that never declared one.
    monkeypatch.delenv("LIVESTACK_RELAYS", raising=False)
    assert "relays" not in HostBroker(
        devices=[], peers=[], clock=lambda: 1000.0).fleet_view()


def test_a_relay_rank_end_to_end_over_http(monkeypatch):
    """The full surface: /fleet/rank?via=relay:… against a broker whose fleet
    view carries the declared relays."""
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")

    view = {**VIEW, "relays": RELAYS}

    class _Broker:
        peers = []
        fleet_principals = None

        def fleet_view(self):
            return view

        def emit_rank(self, result):
            self.last = result

        def membership_snapshot(self):
            return []

    client = TestClient(build_app(_Broker()), raise_server_exceptions=False)
    body = client.get("/fleet/rank", params={"kind": "llm",
                                             "via": "relay:na-public-la"}).json()
    assert body["vantage_used"] == "relay:na-public-la"
    cn = next(t for t in body["targets"]
              if t["target_id"] == "http://100.64.0.3:8100")
    assert cn["distance_band"] != "unknown"
    assert cn["distance_ms"] == 61.0
