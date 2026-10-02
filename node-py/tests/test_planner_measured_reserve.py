"""The device reserve is not charged on top of a measured footprint.

`Device.reserved` covers activation memory that declared, weights-only
footprints omit. A footprint the engine measured already contains it. On
xc-tower-ubuntu (2026-09-28) the 27B's card measures 25.30e9 B with the default
2 GB reserve; the engine measured 25.24e9 B held and 24.61e9 B minimum. Charging
the reserve on top made the unit unplaceable on its own card, and a resident
one would have been shed by step 0 (`free` < 0).
Numbers are the production ones from HARMONY.md, "Unit composition"."""
from livestack_node.hostbroker import RestPeer
from livestack_node.planner import (Defer, Device, Evict, Grant, Load, Placement, Request,
                                    Residency, Unit, WorldState, plan)

CARD = 25_296_044_032
RESERVE = 2_000_000_000
HELD, MIN = 25_243_670_282, 24_614_586_656
DEV = "xc-tower-ubuntu/a46c4c2e"


def llm(source="vllm-startup", admission=True):
    return Unit("llm_general", {"vram_bytes": HELD}, priority=30, residency=Residency.UNPINNED,
                footprint_source=source,
                admission_footprint={"vram_bytes": MIN} if admission else {})


def world(units, placements=(), requests=()):
    return WorldState(devices=(Device(DEV, "xc-tower-ubuntu", capacity={"vram_bytes": CARD},
                                      reserved={"vram_bytes": RESERVE}),),
                      units={u.kind: u for u in units}, placements=tuple(placements),
                      requests=tuple(requests), now=10_000.0)


OTHER = "xc-tower-ubuntu/4bac2869"


def busy_world(llm_unit):
    """Production's shape: the 27B alone on its card, and a request pending
    for an unrelated unit on the OTHER card. A pending request lifts the
    sole-tenant guard, so step 0 then sheds any device whose free is < 0."""
    tts = Unit("tts", {"vram_bytes": 4_000_000_000}, priority=40, servable_on=frozenset({OTHER}))
    return WorldState(
        devices=(Device(DEV, "h", capacity={"vram_bytes": CARD}, reserved={"vram_bytes": RESERVE}),
                 Device(OTHER, "h", capacity={"vram_bytes": CARD}, reserved={"vram_bytes": RESERVE})),
        units={"llm_general": llm_unit, "tts": tts},
        placements=(Placement("llm_general", DEV, loaded_at=0),),
        requests=(Request("r1", "tts", created_at=0),), now=10_000.0)


def test_a_resident_measured_unit_is_not_shed():
    p = plan(busy_world(llm()))
    assert not p.of(Evict), p.summary()
    assert p.of(Grant)                  # the unrelated request is still served


def test_negative_control_the_double_count_sheds_it():
    # The same number, labelled declared, still charges the reserve. A request
    # on another device must not empty this device merely because the static
    # accounting is over budget. Admission on the requested device still works.
    p = plan(busy_world(llm(source="declared", admission=False)))
    assert not p.of(Evict), p.summary()


def test_a_measured_unit_is_admitted_onto_its_empty_card():
    p = plan(world([llm()], requests=[Request("r1", "llm_general", created_at=0)]))
    assert p.of(Load) and p.of(Grant), p.summary()


def test_negative_control_declared_at_the_same_size_is_refused():
    p = plan(world([llm(source="declared", admission=False)],
                   requests=[Request("r1", "llm_general", created_at=0)]))
    assert not p.of(Grant) and p.of(Defer)


def test_one_declared_tenant_brings_the_reserve_back():
    embed = Unit("embed", {"vram_bytes": 500_000_000}, priority=50, residency=Residency.HARD_PIN,
                 footprint_source="declared")
    p = plan(world([llm(), embed], [Placement("embed", DEV, loaded_at=0)],
                   [Request("r1", "llm_general", created_at=0)]))
    # 25.30e9 - 2e9 reserve - 0.5e9 embed < 24.61e9: the reserve protects the
    # embedder's unmodelled activation, so the 27B does not fit beside it.
    assert not p.of(Grant)


class _Peer(RestPeer):
    def __init__(self, units, capacity=CARD):
        super().__init__("http://node/livestack")
        self._payload = {"host_id": "h", "device_id": DEV, "units": units,
                         "device_mem": {"capacity": {"vram_bytes": capacity},
                                        "free": {"vram_bytes": 1}}}

    def _http(self, url, body=None, timeout=5, headers=None):
        return self._payload


def _residence(**over):
    u = {"kind": "llm_general", "footprint": {"vram_bytes": 22_548_578_304}, "residency": 2,
         "resident": True, "busy": False, "footprint_source": "declared",
         "measured": {"footprint": HELD, "min_footprint": MIN, "source": "vllm-startup"}}
    u.update(over)
    return u


def test_rest_peer_adopts_the_measurement_for_residence_and_admission():
    u = _Peer([_residence()]).units()["llm_general"]
    assert u.footprint == {"vram_bytes": float(HELD)}
    assert u.admission_footprint == {"vram_bytes": float(MIN)}
    assert u.footprint_source == "vllm-startup" and u.activation_headroom == {}


def test_rest_peer_keeps_the_prior_when_the_report_did_not_parse():
    u = _Peer([_residence(measured={"measured": "unknown", "unmatched": ["kv_cache_size"]})]).units()["llm_general"]
    assert u.footprint == {"vram_bytes": 22_548_578_304}
    assert u.admission_footprint == {} and u.footprint_source == "declared"
