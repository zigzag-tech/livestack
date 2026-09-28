"""The parser reads real vLLM 0.28.0 startup logs from xc-tower-ubuntu (2026-09-27).

Fixtures are verbatim journal lines, not hand-written, so a format drift in a
future engine shows up as a new fixture failing rather than a guess passing."""
from pathlib import Path

from livestack_node.vllm_startup import (
    GIB, CompositionKey, MeasuredCost, Unknown, composition_hash, key_from_launch, parse)

FIX = Path(__file__).parent / "fixtures" / "vllm_startup"


def _lines(name):
    return (FIX / name).read_text().splitlines()


def test_bf16_one_adapter():
    m = parse(_lines("v0.28.0-llm_general-bf16.log"))
    assert isinstance(m, MeasuredCost)
    assert m.engine_version == "0.28.0"
    assert abs(m.weights_nontorch / GIB - 18.18) < 0.005
    assert abs(m.peak_activation / GIB - 2.23) < 0.005
    assert abs(m.cuda_graphs / GIB - 0.46) < 0.005
    assert m.kv_tokens == 29749
    assert m.max_concurrency_at == 24576 and m.max_concurrency == 1.21
    assert abs(m.budget / GIB - 22.62) < 0.005


def test_fp8_two_adapters():
    m = parse(_lines("v0.28.0-llm_general-fp8.log"))
    assert isinstance(m, MeasuredCost)
    assert m.kv_tokens == 37981
    assert abs(m.kv_bytes / GIB - 1.66) < 0.005
    assert abs(m.cuda_graphs / GIB - 0.90) < 0.005
    # What the card holds (KV fills the budget) vs the least the engine needs
    # (KV for one 24,576-token request): 23.51 vs ~22.9 GiB. Both above the
    # declared 21 GB; neither is the admission number yet (design §8b).
    assert m.footprint / GIB > 23.0
    per_token = m.kv_bytes / m.kv_tokens
    assert m.min_footprint == int(m.weights_nontorch + m.peak_activation + m.cuda_graphs
                                  + per_token * 24576)
    assert 22.5 < m.min_footprint / GIB < m.footprint / GIB


def test_truncated_log_is_unknown_naming_the_missing_lines():
    lines = [l for l in _lines("v0.28.0-llm_general-fp8.log") if "GPU KV cache size" not in l]
    m = parse(lines)
    assert isinstance(m, Unknown)
    assert m.unmatched == ("kv_cache_size",)
    assert m.to_json()["measured"] == "unknown"


def test_empty_output_is_unknown_not_zero():
    m = parse([])
    assert isinstance(m, Unknown)
    assert set(m.unmatched) == {"kv_cache_memory", "kv_cache_size", "actual_usage"}


def test_composition_hash_ignores_adapter_order_and_tracks_every_field():
    a = CompositionKey(base="q27", adapters=(("jemm", 16), ("chips", 16)), kv_dtype="fp8",
                       max_model_len=24576, max_num_seqs=32, engine_version="0.28.0")
    b = CompositionKey(base="q27", adapters=(("chips", 16), ("jemm", 16)), kv_dtype="fp8",
                       max_model_len=24576, max_num_seqs=32, engine_version="0.28.0")
    assert composition_hash(a) == composition_hash(b)
    for change in ({"kv_dtype": "auto"}, {"max_model_len": 20000}, {"max_num_seqs": 16},
                   {"adapters": (("chips", 16),)}, {"engine_version": "0.29.0"}, {"base": "q8"}):
        other = CompositionKey(**{**a.__dict__, **change})
        assert composition_hash(other) != composition_hash(a), change


def test_key_from_launch_reads_the_flags_harmony_passes():
    args = ["--max-num-seqs", "32", "--kv-cache-dtype", "fp8", "--max-model-len", "24576"]
    k = key_from_launch("q27", args, {"jemm": 16, "chips": 16}, "0.28.0")
    assert k.kv_dtype == "fp8" and k.max_model_len == 24576 and k.max_num_seqs == 32
    assert k.adapters == (("chips", 16), ("jemm", 16))
    assert key_from_launch("q27", [], {}).kv_dtype == "auto"
