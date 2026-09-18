"""Region: declared by the node, applied by the caller.

The fleet broker ranks by MEASURED DISTANCE and records the asker's region
without applying it — that split is deliberate and documented in
`fleet_rank.py` and `hostd.fleet_rank`. These pin down the other half: that a
node can say where it is, that the fact survives the announce and the
snapshot, and that a caller can refuse everything outside its own policy.

The numbers in the fixtures are real ones from the fleet, measured from
xc-tower-ubuntu: 2.7 ms to a node on the same host, 6.6 ms to xc-mac-studio,
546 ms to zz-tower0. That gap is why distance alone almost does the job — and
"almost" is why region exists: distance answers "how far from here", never
"where is it", and the second question is the one that has an answer when the
asker moves.
"""
import time

import pytest

from livestack_node.announce import node_region, register_once
from livestack_node.client import (
    NoEligibleTarget,
    choose,
    eligible_targets,
)
from livestack_node.fleet_rank import rank
from livestack_node.membership import PeerRoster


def view(nodes):
    """A fleet view with one host per node, as `/fleet` returns it."""
    hosts = {}
    for n in nodes:
        hosts.setdefault(n["host_id"], {"nodes": []})["nodes"].append(n)
    return {"generated_at": 1000.0, "hosts": hosts}


NA_TTS = {"host_id": "xc-tower-ubuntu", "peer": "http://100.64.0.18:8100/livestack",
          "state": "fresh", "ready": True, "kinds": ["polytts"], "region": "na",
          "probe_ms": 2.7}
MAC_TTS = {"host_id": "xc-mac-studio", "peer": "http://100.64.0.2:8100/livestack",
           "state": "fresh", "ready": True, "kinds": ["polytts"], "region": "na",
           "probe_ms": 6.6}
CN_TTS = {"host_id": "zz-tower0", "peer": "http://100.64.0.3:8100/livestack",
          "state": "fresh", "ready": True, "kinds": ["polytts"], "region": "cn",
          "probe_ms": 545.9}
UNLABELLED_TTS = {"host_id": "mystery", "peer": "http://10.0.0.9:8100/livestack",
                  "state": "fresh", "ready": True, "kinds": ["polytts"], "probe_ms": 1.0}


class TestNodeDeclaresIt:
    def test_region_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("LIVESTACK_NODE_REGION", " NA ")
        assert node_region() == "na"

    def test_unset_is_none_not_empty_string(self, monkeypatch):
        # `None` and `""` diverge at the filter: one is "unknown" and the other
        # would compare unequal to every region and look like a typo.
        monkeypatch.delenv("LIVESTACK_NODE_REGION", raising=False)
        assert node_region() is None
        monkeypatch.setenv("LIVESTACK_NODE_REGION", "   ")
        assert node_region() is None

    def test_announce_carries_it(self):
        sent = {}

        def fake_urlopen(req, timeout=None):  # pragma: no cover - trivial shim
            raise AssertionError("not used")

        import json as _json

        def capture(facade_url, **kw):
            sent.update(kw)
            return {}

        # register_once builds the body; exercise it through a broker stub.
        import urllib.request as _ur

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        posted = []

        def urlopen(req, timeout=None):
            posted.append(_json.loads(req.data.decode()))
            return _Resp()

        old = _ur.urlopen
        _ur.urlopen = urlopen
        try:
            register_once("http://n:1/livestack", host_id="h", kind="polytts",
                          region="na", broker="http://b:8799")
        finally:
            _ur.urlopen = old

        assert posted[0]["region"] == "na"

    def test_an_unset_region_is_omitted_rather_than_nulled(self):
        # A null on every renewal would overwrite a region the broker already
        # knew from a seed.
        import json as _json
        import urllib.request as _ur

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        posted = []
        old = _ur.urlopen
        _ur.urlopen = lambda req, timeout=None: (posted.append(_json.loads(req.data.decode())), _Resp())[1]
        try:
            register_once("http://n:1/livestack", host_id="h", kind="polytts",
                          broker="http://b:8799")
        finally:
            _ur.urlopen = old
        assert "region" not in posted[0]


class TestItSurvivesMembership:
    def test_the_roster_keeps_it_and_the_snapshot_shows_it(self):
        roster = PeerRoster(clock=lambda: 100.0)
        roster.register("http://n:1/livestack", host_id="h", kinds=["polytts"], region="na")
        row = roster.snapshot()[0]
        assert row["region"] == "na"

    def test_a_renewal_without_a_region_does_not_erase_it(self):
        # `_upsert` skips None values; this is the test that keeps it that way.
        roster = PeerRoster(clock=lambda: 100.0)
        roster.register("http://n:1/livestack", host_id="h", region="na")
        roster.register("http://n:1/livestack", host_id="h")
        assert roster.snapshot()[0]["region"] == "na"


class TestRankingCarriesItAndIgnoresIt:
    def test_every_target_reports_its_region(self):
        result = rank(view([NA_TTS, CN_TTS]), "polytts", now=1001.0)
        by_id = {t["target_id"]: t for t in result["targets"]}
        assert by_id["http://100.64.0.18:8100"]["region"] == "na"
        assert by_id["http://100.64.0.3:8100"]["region"] == "cn"

    def test_the_broker_still_orders_on_distance_alone(self):
        # Region is NOT a ranking input. If it ever becomes one, the broker has
        # started deciding policy, which is the thing its own design forbids.
        result = rank(view([CN_TTS, NA_TTS]), "polytts", now=1001.0)
        assert result["chosen"] == "http://100.64.0.18:8100"
        assert "band<50" in result["reason"]


class TestTheCallerAppliesThePolicy:
    def ranking(self, nodes, **kw):
        return rank(view(nodes), "polytts", now=1001.0, **kw)

    def test_china_is_refused_even_when_it_is_the_only_one_left(self):
        # The whole point: a North-American caller with nothing near it does
        # not quietly reach across the Pacific.
        kept, rejected = eligible_targets(self.ranking([CN_TTS]),
                                          allow_regions={"na"}, now=1001.0)
        assert kept == []
        assert "region cn" in rejected[0]["why"]

    def test_north_america_wins_and_keeps_the_broker_order(self):
        kept, _ = eligible_targets(self.ranking([CN_TTS, MAC_TTS, NA_TTS]),
                                   allow_regions={"na"}, now=1001.0)
        assert [t["target_id"] for t in kept] == [
            "http://100.64.0.18:8100", "http://100.64.0.2:8100"]

    def test_an_unlabelled_node_is_excluded_by_default(self):
        # It is 1 ms away and it has not said where it is. Silence is not a
        # match — that is how one unlabelled node in the wrong country becomes
        # the nearest thing a caller will accept.
        kept, rejected = eligible_targets(self.ranking([UNLABELLED_TTS, NA_TTS]),
                                          allow_regions={"na"}, now=1001.0)
        assert [t["target_id"] for t in kept] == ["http://100.64.0.18:8100"]
        assert rejected[0]["why"] == "no region declared"

    def test_unlabelled_can_be_admitted_deliberately(self):
        kept, _ = eligible_targets(self.ranking([UNLABELLED_TTS]),
                                   allow_regions={"na"}, allow_unknown_region=True,
                                   now=1001.0)
        assert len(kept) == 1

    def test_no_policy_keeps_everything(self):
        kept, rejected = eligible_targets(self.ranking([CN_TTS, NA_TTS]), now=1001.0)
        assert len(kept) == 2 and rejected == []

    def test_a_stale_ranking_is_worth_nothing(self):
        # `fleet_rank`: a stale ranking is worse than none, because none falls
        # back to a working default while stale looks authoritative.
        ranking = self.ranking([NA_TTS])
        kept, rejected = eligible_targets(ranking, allow_regions={"na"},
                                          now=1001.0 + ranking["ttl_s"] + 1)
        assert kept == []
        assert "past its" in rejected[0]["why"]

    def test_inside_the_ttl_it_is_fine(self):
        ranking = self.ranking([NA_TTS])
        kept, _ = eligible_targets(ranking, allow_regions={"na"},
                                   now=1001.0 + ranking["ttl_s"] - 1)
        assert len(kept) == 1


class TestChoose:
    def test_it_returns_the_endpoint_and_says_what_it_refused(self, monkeypatch):
        ranking = rank(view([CN_TTS, NA_TTS]), "polytts", now=1001.0)
        monkeypatch.setattr("livestack_node.client.rank_snapshot",
                            lambda *a, **k: ranking)
        got = choose("polytts", allow_regions={"na"}, now=1001.0)
        assert got["endpoint"] == "http://100.64.0.18:8100"
        assert got["region"] == "na"
        # The broker's own pick is reported alongside, so a reader can see when
        # policy changed the answer.
        assert got["broker_choice"] == "http://100.64.0.18:8100"
        assert any("region cn" in r["why"] for r in got["rejected"])

    def test_it_refuses_rather_than_reaching_outside_the_policy(self, monkeypatch):
        ranking = rank(view([CN_TTS]), "polytts", now=1001.0)
        monkeypatch.setattr("livestack_node.client.rank_snapshot",
                            lambda *a, **k: ranking)
        with pytest.raises(NoEligibleTarget) as excinfo:
            choose("polytts", allow_regions={"na"}, now=1001.0)
        # The error names what was rejected and why; "no TTS anywhere" and
        # "every TTS is in the wrong region" want opposite responses.
        assert "region cn" in str(excinfo.value)

    def test_a_broker_nobody_answers_is_an_error_not_a_silent_default(self):
        with pytest.raises(NoEligibleTarget) as excinfo:
            choose("polytts", allow_regions={"na"},
                   brokers=["http://127.0.0.1:1"], timeout=0.2)
        assert "no broker answered" in str(excinfo.value)
