"""ActivationTracker (peak-activation learning) + RestPeer parse of the measured
activation_headroom the node reports. Pure logic — no GPU: observe() is driven with
byte scalars standing in for the allocator high-water."""
from livestack_node.measure import ActivationTracker
from livestack_node.hostbroker import RestPeer


def test_tracker_attributes_excess_to_sole_busy_unit_high_water():
    t = ActivationTracker()
    # process peaked at 12; resident weights (asr) = 5 => 7 is asr's activation.
    t.observe(peak_bytes=12, resident_weights_bytes=5, busy_units={"asr"})
    assert t.headroom_bytes("asr") == 7
    # a smaller later window never lowers the reserve...
    t.observe(peak_bytes=9, resident_weights_bytes=5, busy_units={"asr"})
    assert t.headroom_bytes("asr") == 7
    # ...a bigger input raises it (one long chunk pins the reserve up).
    t.observe(peak_bytes=20, resident_weights_bytes=5, busy_units={"asr"})
    assert t.headroom_bytes("asr") == 15


def test_tracker_skips_ambiguous_idle_or_meterless_windows():
    t = ActivationTracker()
    t.observe(10, 2, set())          # nobody busy -> can't attribute
    t.observe(10, 2, {"a", "b"})     # two busy -> smearing avoided
    t.observe(None, 2, {"a"})        # meter unavailable
    assert t.headroom_bytes("a") == 0
    assert t.headroom_bytes("b") == 0


def test_tracker_never_negative_when_peak_below_weights():
    t = ActivationTracker()
    t.observe(peak_bytes=3, resident_weights_bytes=10, busy_units={"asr"})
    assert t.headroom_bytes("asr") == 0


def test_tracker_persists_high_water_across_restart(tmp_path):
    store = str(tmp_path / "act.json")
    t = ActivationTracker(store_path=store)
    t.observe(peak_bytes=20, resident_weights_bytes=5, busy_units={"align"})
    assert t.headroom_bytes("align") == 15
    # a fresh tracker (simulating a process restart) seeds from the store — the peak
    # is remembered, not re-learned via another OOM.
    t2 = ActivationTracker(store_path=store)
    assert t2.headroom_bytes("align") == 15
    # and it keeps ratcheting up from the persisted floor.
    t2.observe(peak_bytes=30, resident_weights_bytes=5, busy_units={"align"})
    assert ActivationTracker(store_path=store).headroom_bytes("align") == 25


def test_tracker_survives_corrupt_or_missing_store(tmp_path):
    missing = str(tmp_path / "nope.json")
    assert ActivationTracker(store_path=missing).headroom_bytes("x") == 0  # no crash
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    t = ActivationTracker(store_path=str(bad))   # corrupt -> start fresh, don't raise
    assert t.headroom_bytes("x") == 0
    t.observe(peak_bytes=9, resident_weights_bytes=2, busy_units={"x"})
    assert t.headroom_bytes("x") == 7


def test_restpeer_parses_activation_headroom_into_unit():
    p = RestPeer("http://node", priorities={}, footprints={})
    snap = {"host_id": "h", "device_id": "h/gpu0", "units": [
        {"kind": "asr", "footprint": {"vram_bytes": 5}, "residency": 0,
         "resident": True, "busy": True,
         "activation_headroom": {"vram_bytes": 7}},
        {"kind": "chipgen", "footprint": {"vram_bytes": 5}, "residency": 2,
         "resident": False, "busy": False},   # node reported no headroom
    ]}
    p.refresh = lambda: snap
    p._snap = snap
    units = p.units()
    assert units["asr"].activation_headroom == {"vram_bytes": 7}
    assert units["chipgen"].activation_headroom == {}   # absent -> empty, unchanged
