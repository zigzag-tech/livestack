"""Node reports carry the host-pool and whole-device facts — and an OLD report
parses unchanged.

A unit that pins host RAM reports a footprint VECTOR (`ram_bytes` beside
`vram_bytes`); a unit that claims a whole device reports `exclusive_device`;
both carry the engine name and pinned rev for the broker's ledger record. A
report that predates the fields must parse to exactly the unit it always did —
and the placement record must carry what design §6 requires.
"""
from livestack_node.hostbroker import HostBroker, RestPeer, aggregate_units
from livestack_node.ledger import JsonlLedger
from livestack_node.planner import Grant, Request, Residency, Unit

GB = 1 << 30


def _peer_with(snap):
    class _FixturePeer(RestPeer):
        def _http(self, *_args, **_kw):
            return snap
    return _FixturePeer("http://node/livestack")


def _report(unit_rows):
    return {
        "host_id": "h1", "node_id": "http://node/livestack", "device_id": "gpu0",
        "device_candidates": ["gpu0"],
        "units": unit_rows,
        "device_mem": {"capacity": {"vram_bytes": 24 * GB},
                       "free": {"vram_bytes": 24 * GB}},
        "host_mem": {"total_bytes": 94 * GB, "available_bytes": 64 * GB,
                     "process_rss_bytes": 1 * GB},
    }


OLD_UNIT = {"kind": "llm_general", "footprint": {"vram_bytes": 21 * GB},
            "residency": int(Residency.UNPINNED), "resident": True, "busy": False,
            "footprint_source": "declared"}

NEW_UNIT = {"kind": "flash_next",
            "footprint": {"vram_bytes": 20 * GB, "ram_bytes": 45 * GB},
            "residency": int(Residency.UNPINNED), "resident": False, "busy": False,
            "footprint_source": "declared",
            "exclusive_device": True, "engine": "strata", "engine_rev": "36fa455",
            "measured": {"measured": "yes", "footprint": 20 * GB + 500 * (1 << 20),
                         "min_footprint": 18 * GB, "source": "strata-startup"}}


def test_an_old_unit_report_parses_unchanged():
    u = _peer_with(_report([OLD_UNIT])).units()["llm_general"]
    assert u.footprint == {"vram_bytes": 21 * GB}
    assert u.exclusive_device is False
    assert u.engine == ""
    assert u.engine_rev == ""


def test_new_report_fields_populate_the_planner_unit():
    u = _peer_with(_report([OLD_UNIT, NEW_UNIT])).units()["flash_next"]
    # The engine's report replaces the DEVICE number only; `ram_bytes` survives
    # it (it is charged to the host pool whatever the card looks like).
    assert u.footprint == {"vram_bytes": 20 * GB + 500 * (1 << 20),
                           "ram_bytes": 45 * GB}
    assert u.admission_footprint == {"vram_bytes": 18 * GB}
    assert u.footprint_source == "strata-startup"
    assert u.exclusive_device is True
    assert u.engine == "strata"
    assert u.engine_rev == "36fa455"


def test_the_placement_record_carries_the_who_and_the_why(tmp_path):
    led = JsonlLedger(str(tmp_path / "host.jsonl"))
    br = HostBroker(peers=[_peer_with(_report([OLD_UNIT, NEW_UNIT]))],
                    clock=lambda: 100.0, ledger=led, emitter_id="h:8799")
    p = br.plan_and_apply([Request("r1", "flash_next", created_at=100.0)])
    assert [g.kind for g in p.of(Grant)] == ["flash_next"]
    recs = [r for r in led.read() if r["decision"] in ("load", "grant") and r["kind"] == "flash_next"]
    assert recs, "the placement emitted no record"
    got = {k: v for r in recs for k, v in (r.get("outcome") or {}).items()}
    assert got["engine"] == "strata"
    assert got["engine_rev"] == "36fa455"
    assert got["exclusive_device"] is True
    # The host-pool arithmetic the record must carry: need 45, free after the
    # pin 18 (64 measured - 1 reserve - 45 loaded), reserve 1.
    assert got["host_pool"]["need"] == {"ram_bytes": 45 * GB}
    assert got["host_pool"]["free"] == {"ram_bytes": 18 * GB}
    assert got["host_pool"]["reserve"] == {"ram_bytes": 1 * GB}


def test_a_unit_without_ram_bytes_leaves_no_host_pool_record(tmp_path):
    led = JsonlLedger(str(tmp_path / "host.jsonl"))
    br = HostBroker(peers=[_peer_with(_report([OLD_UNIT]))],
                    clock=lambda: 100.0, ledger=led, emitter_id="h:8799")
    p = br.plan_and_apply([Request("r1", "llm_general", created_at=100.0)])
    assert p.of(Grant), "expected a grant"
    recs = [r for r in led.read() if r["decision"] in ("load", "grant")]
    assert recs
    assert all("host_pool" not in (r.get("outcome") or {}) for r in recs)


def test_aggregated_peers_fold_the_new_fields():
    a = Unit("llm", {"vram_bytes": 20 * GB, "ram_bytes": 45 * GB},
             exclusive_device=True, engine="strata", engine_rev="36fa455")
    b = Unit("llm", {"vram_bytes": 22 * GB, "ram_bytes": 40 * GB})
    merged = aggregate_units({("llm", "p1"): a, ("llm", "p2"): b},
                             {"p1": "gpu0", "p2": "gpu1"})
    u = merged["llm"]
    assert u.exclusive_device is True            # the stronger claim wins
    assert (u.engine, u.engine_rev) == ("strata", "36fa455")
    assert u.footprint == {"vram_bytes": 22 * GB, "ram_bytes": 45 * GB}
