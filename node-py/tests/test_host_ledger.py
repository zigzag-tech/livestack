"""The shared host-RAM ledger: one file per claim, refreshed or it lapses."""
from livestack_node.host_ledger import HostLedger


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_an_entry_lives_until_its_writer_stops_refreshing(tmp_path):
    clock = _Clock()
    led = HostLedger(tmp_path, clock=clock)
    led.put("load", "flash_next", ttl_s=30, ram_bytes=45 << 30, state="waiting")
    assert [e["key"] for e in led.entries("load")] == ["flash_next"]
    clock.t += 31
    assert led.entries("load") == []
    assert not list(tmp_path.glob("load-*.json"))       # lapsed entries are swept


def test_an_attempts_outstanding_claim_is_what_its_cgroup_has_not_touched(tmp_path):
    led = HostLedger(tmp_path, clock=_Clock())
    led.put("attempt", "a" * 32, ttl_s=60, claim_bytes=10 << 30)
    led.put("attempt", "b" * 32, ttl_s=60, claim_bytes=4 << 30)
    # a holds 2 GiB of its 10; b has no cgroup yet (still preparing): whole claim
    assert led.attempt_outstanding_bytes({"a" * 32: 2 << 30}) == (8 << 30) + (4 << 30)
    # an attempt past its claim owes nothing more, never a negative
    assert led.attempt_outstanding_bytes({"a" * 32: 11 << 30, "b" * 32: 4 << 30}) == 0


def test_planned_services_and_loads_read_back(tmp_path):
    led = HostLedger(tmp_path, clock=_Clock())
    led.put("service", "system.slice/harmony-llm.service", ttl_s=120, units=["flash_next"])
    led.put("load", "flash_next", ttl_s=30, ram_bytes=45 << 30, state="loading", reason="x")
    assert led.planned_services() == {"system.slice/harmony-llm.service": ["flash_next"]}
    assert led.loads() == [{"unit": "flash_next", "ram_bytes": 45 << 30,
                            "state": "loading", "reason": "x"}]
    led.drop("load", "flash_next")
    assert led.loads() == []


def test_garbage_is_skipped_and_an_absent_ledger_is_empty(tmp_path):
    assert HostLedger(tmp_path / "nowhere").entries() == []
    (tmp_path / "load-x.json").write_text("{not json")
    (tmp_path / "load-y.json").write_text('{"kind": "load"}')        # no expiry
    assert HostLedger(tmp_path).entries() == []


def test_a_key_cannot_escape_the_ledger_directory(tmp_path):
    led = HostLedger(tmp_path, clock=_Clock())
    led.put("service", "../../etc/passwd", ttl_s=60, units=[])
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())
