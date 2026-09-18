import json

import pytest

from livestack_node.policy_lab.overhead import (
    MIN_OVERHEAD_SAMPLES,
    OverheadMeasurement,
    evaluate_overhead,
    measure_observer_overhead,
)


def test_overhead_gate_requires_samples_bounds_and_no_producer_io():
    passing = evaluate_overhead(
        OverheadMeasurement(
            samples=MIN_OVERHEAD_SAMPLES,
            baseline_p95_ns=1_000_000,
            enabled_p95_ns=5_999_999,
            queued_bytes=100,
            max_queue_bytes=200,
            dropped_events=0,
            rejected_events=0,
            producer_io_calls=0,
        )
    )
    assert passing["status"] == "passed"
    assert passing["added_p95_ms"] == pytest.approx(4.999999)
    assert passing["allowed_added_p95_ms"] == 5.0

    assert evaluate_overhead(
        OverheadMeasurement(**{**passing["measurement"], "samples": MIN_OVERHEAD_SAMPLES - 1})
    )["status"] == "insufficient_evidence"
    assert evaluate_overhead(
        OverheadMeasurement(**{**passing["measurement"], "queued_bytes": 201})
    )["status"] == "failed"
    assert evaluate_overhead(
        OverheadMeasurement(**{**passing["measurement"], "producer_io_calls": 1})
    )["status"] == "failed"


def test_real_isolated_streaming_measurement_is_bounded_and_reports_raw_delta():
    report = measure_observer_overhead(samples=MIN_OVERHEAD_SAMPLES, warmup=50)
    assert report["schema_version"] == 1
    assert report["kind"] == "observer_overhead_report"
    assert report["scope"] == "isolated_python_streaming_callback"
    assert report["measurement"]["samples"] == MIN_OVERHEAD_SAMPLES
    assert report["measurement"]["queued_bytes"] <= report["measurement"]["max_queue_bytes"]
    assert report["measurement"]["producer_io_calls"] == 0
    assert report["measurement"]["dropped_events"] == 0
    assert report["added_p95_ms"] == pytest.approx(
        (report["measurement"]["enabled_p95_ns"] - report["measurement"]["baseline_p95_ns"])
        / 1_000_000
    )
    json.dumps(report, allow_nan=False)
