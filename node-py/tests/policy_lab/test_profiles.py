from dataclasses import replace

from livestack_node.policy_lab.profiles import (
    Estimate,
    ProfileKey,
    ProfilePack,
    ProfilePoint,
    keyed_uniform,
)


def _key(**changes):
    base = ProfileKey(
        operation="llm_prefill",
        model_revision="model-a",
        runtime_revision="runtime-a",
        hardware_revision="gpu-a",
        cache_tier="miss",
        concurrency=1,
        interference="isolated",
        work_units=100,
    )
    return replace(base, **changes)


def _point(key, p50):
    return ProfilePoint(
        key=key,
        estimate=Estimate(
            p50_us=p50,
            p95_us=p50 * 2,
            lower_us=p50 // 2,
            upper_us=p50 * 3,
            sample_count=100,
            measured_at_utc_us=1,
        ),
    )


def test_exact_profile_identity_and_visible_mismatch():
    pack = ProfilePack([_point(_key(), 1_000)])
    exact = pack.lookup(_key())
    assert exact.status == "exact"
    assert exact.qualified
    assert exact.estimate.p50_us == 1_000

    missing = pack.lookup(_key(runtime_revision="runtime-b"))
    assert missing.status == "missing"
    assert not missing.qualified
    assert "runtime_revision" in missing.mismatched_dimensions
    assert missing.estimate is None


def test_concurrency_cache_and_hardware_mismatch_never_become_zero_cost():
    pack = ProfilePack([_point(_key(), 1_000)])
    for query in (
        _key(concurrency=2),
        _key(cache_tier="hit"),
        _key(hardware_revision="gpu-b"),
    ):
        result = pack.lookup(query)
        assert result.status == "missing"
        assert result.estimate is None


def test_supported_interpolation_is_inside_envelope_only():
    pack = ProfilePack([_point(_key(work_units=100), 1_000), _point(_key(work_units=200), 2_000)])
    middle = pack.lookup(_key(work_units=150), allow_interpolation=True)
    assert middle.status == "interpolated"
    assert middle.qualified
    assert middle.estimate.p50_us == 1_500

    outside = pack.lookup(_key(work_units=250), allow_interpolation=True)
    assert outside.status == "extrapolation_unsupported"
    assert not outside.qualified
    assert outside.estimate is None


def test_keyed_random_stream_is_independent_of_call_order_and_unrelated_requests():
    key = ("scenario", "request-a", "load", "revision")
    before = keyed_uniform(7, *key)
    keyed_uniform(7, "scenario", "unrelated", "load", "revision")
    after = keyed_uniform(7, *key)
    assert before == after
    assert before != keyed_uniform(7, "scenario", "request-b", "load", "revision")
