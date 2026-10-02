"""Two nodes serving one kind fold into one planner Unit without losing what
was measured (hostbroker.aggregate_units)."""
from livestack_node.hostbroker import aggregate_units
from livestack_node.planner import Unit

GB = 1_000_000_000


def unit(fp, source, adm=None, act=None):
    return Unit("klein", {"vram_bytes": fp}, footprint_source=source,
                admission_footprint=adm or {}, activation_headroom=act or {})


def test_a_measured_and_a_declared_peer_say_so():
    # zz-joe 2026-10-02: klein-0 measured (4.88 GB), klein-1 never loaded (3 GB).
    out = aggregate_units({("klein", "a"): unit(4.88 * GB, "allocator", act={"vram_bytes": 2.34 * GB}),
                           ("klein", "b"): unit(3 * GB, "declared")})["klein"]
    assert out.footprint == {"vram_bytes": 4.88 * GB}
    assert out.activation_headroom == {"vram_bytes": 2.34 * GB}
    assert out.footprint_source == "allocator+declared"


def test_agreeing_peers_keep_their_source():
    out = aggregate_units({("llm", "a"): unit(20 * GB, "vllm-startup", adm={"vram_bytes": 15 * GB}),
                           ("llm", "b"): unit(21 * GB, "vllm-startup", adm={"vram_bytes": 16 * GB})})["llm"]
    assert out.footprint_source == "vllm-startup"
    assert out.admission_footprint == {"vram_bytes": 16 * GB}


def test_an_admission_need_is_not_dropped_beside_a_peer_without_one():
    out = aggregate_units({("llm", "a"): unit(20 * GB, "vllm-startup", adm={"vram_bytes": 15 * GB}),
                           ("llm", "b"): unit(18 * GB, "declared")})["llm"]
    assert out.admission_footprint == {"vram_bytes": 18 * GB}
    assert out.footprint_source == "declared+vllm-startup"
