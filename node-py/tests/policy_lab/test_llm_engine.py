import pytest

from livestack_node.policy_lab.llm_engine import (
    LLMEngine,
    LLMEngineError,
    LLMRequest,
    PrefixCacheKey,
)


def _request(request_id, **changes):
    values = dict(
        request_id=request_id,
        model_revision="model",
        runtime_revision="runtime",
        tokenizer_revision="tokenizer",
        tenant_scope="tenant-a",
        prompt_tokens=100,
        output_tokens=2,
        prefix_id=None,
        prefix_tokens=0,
        batching_eligible=True,
    )
    values.update(changes)
    return LLMRequest(**values)


def test_s08_prefill_cache_kv_growth_and_conditional_decode_batching():
    engine = LLMEngine(
        context_limit=1000,
        prefill_us_per_token=10,
        decode_step_us_by_batch={1: 100, 2: 120},
        kv_bytes_per_token=4,
        prefix_cache_capacity_tokens=500,
    )
    engine.put_prefix(
        PrefixCacheKey("model", "runtime", "tokenizer", "tenant-a", "prefix"),
        token_count=80,
    )
    long = _request("long", prefix_id="prefix", prefix_tokens=80)
    short = _request("short", prompt_tokens=10)
    assert engine.prefill(long, at_us=0).finished_at_us == 200
    assert engine.prefill(short, at_us=0).finished_at_us == 100
    assert engine.kv_bytes("long") == 400

    step = engine.decode_step(["long", "short"], at_us=200)
    assert step.finished_at_us == 320
    assert set(step.request_ids) == {"long", "short"}
    assert engine.kv_bytes("long") == 404
    assert engine.kv_bytes("short") == 44


def test_s21_prefix_cache_requires_revision_runtime_tokenizer_and_tenant_compatibility():
    engine = LLMEngine(
        context_limit=1000,
        prefill_us_per_token=10,
        decode_step_us_by_batch={1: 100},
        kv_bytes_per_token=4,
        prefix_cache_capacity_tokens=500,
    )
    engine.put_prefix(
        PrefixCacheKey("model", "runtime", "tokenizer", "tenant-a", "prefix"), 80
    )
    mismatched = _request(
        "other", tenant_scope="tenant-b", prefix_id="prefix", prefix_tokens=80
    )
    result = engine.prefill(mismatched, at_us=0)
    assert result.cache_hit_tokens == 0
    assert result.finished_at_us == 1000


def test_context_and_batch_compatibility_are_hard_constraints():
    engine = LLMEngine(
        context_limit=100,
        prefill_us_per_token=10,
        decode_step_us_by_batch={1: 100, 2: 120},
        kv_bytes_per_token=4,
        prefix_cache_capacity_tokens=10,
    )
    with pytest.raises(LLMEngineError, match="context"):
        engine.prefill(_request("too-long", prompt_tokens=100, output_tokens=1), at_us=0)
    engine = LLMEngine(1000, 10, {1: 100, 2: 120}, 4, 10)
    engine.prefill(_request("a"), at_us=0)
    engine.prefill(_request("b", model_revision="other"), at_us=0)
    with pytest.raises(LLMEngineError, match="incompatible"):
        engine.decode_step(["a", "b"], at_us=1000)
    assert "continuous_batch_reordering" in engine.unsupported_effects
