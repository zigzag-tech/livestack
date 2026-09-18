from livestack_node.policy_lab.calibration import evaluate_calibration


def _cell(count=100, *, predicted=1_000_000, observed=1_000_000, windows=3):
    return {
        "cell_id": "tts/canada/first_output",
        "workload_class": "tts",
        "requester_region": "canada",
        "component": "first_output",
        "applicable_budget_us": 2_000_000,
        "samples": [
            {
                "session_id": f"s{i}",
                "window_id": f"w{i % windows}",
                "observed_us": observed,
                "predicted_p50_us": predicted,
                "predicted_p95_us": predicted,
                "interval_lower_us": 900_000,
                "interval_upper_us": 950_000 if i % 10 == 0 else 1_100_000,
                "observed_peak_memory_bytes": 1_000,
                "predicted_peak_memory_bytes": 1_000,
            }
            for i in range(count)
        ],
    }


def _dataset(cell):
    return {
        "schema_version": 1,
        "kind": "calibration_dataset",
        "domain_id": "domain-v1",
        "profile_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "evaluator_sha256": "c" * 64,
        "cells": [cell],
    }


def test_calibration_passes_frozen_error_coverage_memory_and_sample_gates():
    report = evaluate_calibration(_dataset(_cell()))
    assert report["status"] == "calibrated"
    assert report["certificate"]["domain_id"] == "domain-v1"
    assert report["method"]["bootstrap_resamples"] == 1_000
    assert report["cells"][0]["checks"] == {
        "sample_count": True,
        "observation_windows": True,
        "p50_error": True,
        "p95_error": True,
        "interval_coverage": True,
        "interval_width": True,
        "memory_underprediction": True,
    }


def test_calibration_reports_insufficient_samples_and_failed_prediction_separately():
    insufficient = evaluate_calibration(_dataset(_cell(count=99)))
    assert insufficient["status"] == "insufficient_evidence"
    assert not insufficient["cells"][0]["checks"]["sample_count"]

    failed = evaluate_calibration(_dataset(_cell(predicted=2_000_000)))
    assert failed["status"] == "failed"
    assert not failed["cells"][0]["checks"]["p50_error"]
    assert failed["certificate"] is None


def test_unmeasured_or_empty_domain_never_inherits_calibration():
    dataset = _dataset(_cell())
    dataset["cells"] = []
    report = evaluate_calibration(dataset)
    assert report["status"] == "insufficient_evidence"
    assert report["unqualified_domains"] == ["domain-v1"]
