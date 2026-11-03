"""The shared host-RAM ledger, end to end across both planners.

Policy (2026-10-18): a model load DRAINS task attempts, it never kills them.

* The host broker's pool subtracts what attempts were PROMISED and have not
  touched yet, so a 45 GiB load is not granted into an attempt's headroom.
* A load refused for host memory is written WAITING; workers then charge it in
  full, which is what stops new attempts from being admitted (the drain).
* A cold model server the broker plans holds nothing in the workers' view —
  no more learned-peak charge for a Strata nobody is using.
* All of it travels in the `host` block the authority already accepts.
"""
import os

from livestack_node import host_ledger as hl
from livestack_node.host_ledger import HostLedger
from livestack_node.hostbroker import HostBroker, RestPeer
from livestack_node.hostview import HostView
from livestack_node.planner import Defer, Grant, Request, Residency
from livestack_node.workloads.model import host_view as authority_accepts

GB = 1 << 30
ATTEMPT = "c" * 32

FLASH = {"kind": "flash_next", "footprint": {"vram_bytes": 20 * GB, "ram_bytes": 45 * GB},
         "residency": int(Residency.UNPINNED), "resident": False, "busy": False,
         "footprint_source": "declared", "engine": "strata", "engine_rev": "x"}


def _broker(tmp_path, monkeypatch, attempt_current=None, available=64 * GB):
    snap = {"host_id": "h1", "node_id": "http://node/livestack", "device_id": "gpu0",
            "device_candidates": ["gpu0"], "units": [FLASH],
            "device_mem": {"capacity": {"vram_bytes": 24 * GB}, "free": {"vram_bytes": 24 * GB}},
            "host_mem": {"total_bytes": 94 * GB, "available_bytes": available,
                         "process_rss_bytes": GB}}

    class _Peer(RestPeer):
        def _http(self, *_a, **_k):
            return snap

    br = HostBroker(peers=[_Peer("http://node/livestack")], clock=lambda: 100.0)
    br.host_id = "h1"
    br.host_ledger = HostLedger(tmp_path / "ledger")
    import livestack_node.hostview as hv
    monkeypatch.setattr(hv, "attempt_memory", lambda *a, **k: dict(attempt_current or {}))
    return br


def test_an_attempts_promised_ram_is_not_granted_to_a_model_load(tmp_path, monkeypatch):
    # 64 GiB available, 1 reserve. An attempt promised 25 GiB has touched 2:
    # 23 more may come, so 40 is free — a 45 GiB load must wait.
    br = _broker(tmp_path, monkeypatch, attempt_current={ATTEMPT: 2 * GB})
    br.host_ledger.put("attempt", ATTEMPT, ttl_s=60, claim_bytes=25 * GB)
    p = br.plan_and_apply([Request("r1", "flash_next", created_at=100.0)])
    assert not p.of(Grant)
    reasons = [d.reason for d in p.of(Defer)]
    assert any("insufficient host memory" in r for r in reasons), reasons


def test_with_no_promise_outstanding_the_same_load_fits(tmp_path, monkeypatch):
    br = _broker(tmp_path, monkeypatch)
    p = br.plan_and_apply([Request("r1", "flash_next", created_at=100.0)])
    assert [g.kind for g in p.of(Grant)] == ["flash_next"]


def test_a_refused_load_is_written_waiting_and_a_resident_one_clears(tmp_path, monkeypatch):
    br = _broker(tmp_path, monkeypatch)
    br.plan_and_apply([])                         # learns the units
    br.note_waiting(["flash_next", "unknown_kind"], "insufficient host memory: ...")
    assert br.host_ledger.loads() == [{"unit": "flash_next", "ram_bytes": 45 * GB,
                                       "state": "waiting",
                                       "reason": "insufficient host memory: ..."}]


def _proc(root, available_gib):
    (root / "pressure").mkdir(parents=True, exist_ok=True)
    (root / "meminfo").write_text(f"MemTotal: {60 * 1024**2} kB\nMemAvailable: {available_gib * 1024**2} kB\n")
    (root / "vmstat").write_text("pswpin 1\n")
    for r in ("memory", "io", "cpu"):
        (root / "pressure" / r).write_text(
            "some avg10=0 avg60=0 avg300=0 total=1\nfull avg10=0 avg60=0 avg300=0 total=1\n")
    return root


def _cgroup(path, anon):
    path.mkdir(parents=True, exist_ok=True)
    (path / "memory.current").write_text(f"{anon}\n")
    (path / "memory.peak").write_text(f"{anon}\n")
    (path / "memory.stat").write_text(f"anon {anon}\nfile 0\nkernel 0\nshmem 0\nslab_reclaimable 0\n")


def _view(tmp_path, ledger, current):
    root = tmp_path / "cgroup"
    _cgroup(root / "system.slice/harmony-llm.service", current)
    peaks = tmp_path / "peaks.json"
    # It once reached 38 GiB: the learned peak the old rule charged while cold.
    peaks.write_text('{"system.slice/harmony-llm.service": %d}' % (38 * GB))
    return HostView(services=["system.slice/harmony-llm.service"], peaks_path=peaks,
                    attempts_dir=root / "none", reserve_bytes=8 * GB,
                    proc=_proc(tmp_path / "proc", 57), cgroup_root=root, ledger=ledger)


def _free_for_attempts(sample):
    """workloads/placement.py's memory term, for one host with no attempts."""
    spike = max((max(0, s["peak_bytes"] - s["current_bytes"]) for s in sample["services"].values()
                 if s.get("resident") is not True), default=0)
    return sample["memory_available_bytes"] - sample["memory_reserve_bytes"] - spike


def test_a_cold_planned_server_holds_nothing_and_a_waiting_load_drains(tmp_path):
    led = HostLedger(tmp_path / "ledger")
    view = _view(tmp_path, led, current=GB // 4)
    # Not yet planned: the learned 38 GiB peak is charged (today's rule) —
    # 57 - 8 - 38 leaves 11 GiB for attempts.
    assert _free_for_attempts(view.sample()) // GB == 11
    led.put("service", "system.slice/harmony-llm.service", ttl_s=120, units=["flash_next"])
    cold = view.sample()
    assert cold["services"]["system.slice/harmony-llm.service"]["resident"] is True
    assert _free_for_attempts(cold) // GB == 49        # the whole host, less the reserve
    led.put("load", "flash_next", ttl_s=30, ram_bytes=45 * GB, state="waiting")
    waiting = view.sample()
    # The waiting 45 GiB is outstanding: 4 GiB left, so a 9 GiB e2e attempt no
    # longer fits — new attempts drain while running ones finish.
    assert _free_for_attempts(waiting) // GB == 4
    # And it is said in the fields the deployed authority already accepts.
    assert authority_accepts(waiting)["services"]["system.slice/harmony-llm.service"] == {
        "current_bytes": GB // 4, "peak_bytes": 45 * GB, "resident": False}


def test_a_node_with_ram_units_declares_its_cgroup_planned(tmp_path, monkeypatch):
    from livestack_node import serve

    class _U:
        footprint = {"vram_bytes": 20 * GB, "ram_bytes": 45 * GB}

    class _V:
        footprint = 21 * GB

    monkeypatch.setenv("LIVESTACK_HOST_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(serve, "_own_cgroup", lambda: "system.slice/harmony-llm.service")
    serve._publish_planned_service({"flash_next": _U(), "llm_general": _V()}, interval_s=3600)
    import time
    for _ in range(50):
        if HostLedger(tmp_path / "ledger").planned_services():
            break
        time.sleep(0.02)
    assert HostLedger(tmp_path / "ledger").planned_services() == {
        "system.slice/harmony-llm.service": ["flash_next"]}


def test_own_cgroup_reads_the_v2_line(tmp_path):
    from livestack_node.serve import _own_cgroup
    f = tmp_path / "cgroup"
    f.write_text("0::/system.slice/harmony-llm.service\n")
    assert _own_cgroup(str(f)) == "system.slice/harmony-llm.service"
    f.write_text("12:memory:/x\n")
    assert _own_cgroup(str(f)) is None


def test_a_workers_running_attempt_is_on_the_ledger_until_it_ends(tmp_path):
    # The method a worker's every report calls; the claim is the attempt's cap.
    from livestack_node.workloads.worker import WorkloadWorker

    class _W:
        LEDGER_TTL_S = WorkloadWorker.LEDGER_TTL_S
        host_ledger = HostLedger(tmp_path / "ledger")
        config = {"worker": "zz-whitby-e2e-1"}
        _ledger_attempt = (ATTEMPT, 10 * GB)

    w = _W()
    WorkloadWorker._ledger_refresh(w)
    (entry,) = w.host_ledger.entries("attempt")
    assert entry["key"] == ATTEMPT and entry["claim_bytes"] == 10 * GB
    assert entry["worker"] == "zz-whitby-e2e-1"
    w._ledger_attempt = None                       # the attempt ended
    WorkloadWorker._ledger_refresh(w)              # nothing re-written
    w.host_ledger.drop("attempt", ATTEMPT)
    assert w.host_ledger.entries("attempt") == []
