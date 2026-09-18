"""Reproducible isolated measurement of observation callback overhead."""

from __future__ import annotations

import math
import platform
import time
from dataclasses import asdict, dataclass
from typing import Any

from .emitter import BoundedEmitter


MIN_OVERHEAD_SAMPLES = 1_000
_STREAM_CHUNK = bytes(range(256)) * 2


@dataclass(frozen=True)
class OverheadMeasurement:
    samples: int
    baseline_p95_ns: int
    enabled_p95_ns: int
    queued_bytes: int
    max_queue_bytes: int
    dropped_events: int
    rejected_events: int
    producer_io_calls: int


def _p95_ns(values: list[int]) -> int:
    if not values:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]


def _representative_stream_callback(chunk: bytes) -> int:
    """Small deterministic stand-in for a callback consuming one audio/token chunk."""

    return sum(chunk)


def evaluate_overhead(measurement: OverheadMeasurement) -> dict[str, Any]:
    """Apply the predeclared sample, safety and latency gates."""

    baseline_ms = measurement.baseline_p95_ns / 1_000_000
    enabled_ms = measurement.enabled_p95_ns / 1_000_000
    added_ms = enabled_ms - baseline_ms
    allowed_ms = max(baseline_ms * 0.01, 5.0)
    sufficient_samples = measurement.samples >= MIN_OVERHEAD_SAMPLES
    bounded_queue = 0 <= measurement.queued_bytes <= measurement.max_queue_bytes
    no_callback_io = measurement.producer_io_calls == 0
    lossless = measurement.dropped_events == 0 and measurement.rejected_events == 0
    latency_within_limit = added_ms <= allowed_ms
    checks = {
        "sufficient_samples": sufficient_samples,
        "bounded_queue": bounded_queue,
        "no_callback_io": no_callback_io,
        "lossless_at_measured_load": lossless,
        "latency_within_limit": latency_within_limit,
    }
    if not sufficient_samples:
        status = "insufficient_evidence"
    elif all(checks.values()):
        status = "passed"
    else:
        status = "failed"
    return {
        "schema_version": 1,
        "kind": "observer_overhead_report",
        "scope": "isolated_python_streaming_callback",
        "status": status,
        "measurement": asdict(measurement),
        "baseline_p95_ms": baseline_ms,
        "enabled_p95_ms": enabled_ms,
        "added_p95_ms": added_ms,
        "allowed_added_p95_ms": allowed_ms,
        "checks": checks,
        "live_service_performance_claim": False,
    }


def measure_observer_overhead(*, samples: int, warmup: int = 200) -> dict[str, Any]:
    """Measure paired baseline/enabled callbacks; perform no I/O in timed regions."""

    if samples <= 0 or warmup < 0:
        raise ValueError("samples must be positive and warmup nonnegative")
    # This comfortably holds one compact event per measured and warm-up callback.
    max_queue_bytes = (samples + warmup + 1) * 1_024
    emitter = BoundedEmitter(max_queue_bytes=max_queue_bytes, max_event_bytes=1_024)
    payload = {
        "request_id": "overhead-fixture",
        "attempt_id": "attempt-1",
        "workload_class": "tts",
        "produced_units": len(_STREAM_CHUNK),
        "coverage_us": 20_000,
    }

    for index in range(warmup):
        _representative_stream_callback(_STREAM_CHUNK)
        emitter.offer("stream_progress", payload, now_us=index)
    # Warm-up records must not count against the measured queue bound.
    emitter.drain()

    baseline: list[int] = []
    enabled: list[int] = []

    def record_baseline() -> None:
        start = time.perf_counter_ns()
        _representative_stream_callback(_STREAM_CHUNK)
        baseline.append(time.perf_counter_ns() - start)

    def record_enabled(index: int) -> None:
        start = time.perf_counter_ns()
        _representative_stream_callback(_STREAM_CHUNK)
        emitter.offer("stream_progress", payload, now_us=warmup + index)
        enabled.append(time.perf_counter_ns() - start)

    # Alternate order to limit systematic bias from frequency scaling and drift.
    for index in range(samples):
        if index % 2:
            record_enabled(index)
            record_baseline()
        else:
            record_baseline()
            record_enabled(index)

    queued = emitter.drain()
    report = evaluate_overhead(
        OverheadMeasurement(
            samples=samples,
            baseline_p95_ns=_p95_ns(baseline),
            enabled_p95_ns=_p95_ns(enabled),
            queued_bytes=sum(map(len, queued)),
            max_queue_bytes=max_queue_bytes,
            dropped_events=emitter.dropped_events,
            rejected_events=emitter.rejected_events,
            producer_io_calls=emitter.producer_io_calls,
        )
    )
    report["environment"] = {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
    report["method"] = {
        "clock": "time.perf_counter_ns",
        "pairing": "alternating baseline/enabled order",
        "warmup_callbacks": warmup,
        "stream_chunk_bytes": len(_STREAM_CHUNK),
        "percentile": "nearest-rank p95",
    }
    return report
