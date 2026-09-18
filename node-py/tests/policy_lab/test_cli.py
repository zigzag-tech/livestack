import json
import subprocess
import sys


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "livestack_node.policy_lab", *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _dataset():
    return {
        "schema_version": 1,
        "kind": "routing_dataset",
        "requests": [
            {
                "request_id": "r",
                "attempt_id": "a",
                "now_us": 0,
                "deadline_us": 10000,
                "wait_age_us": 0,
                "fairness_bound_us": 1000,
                "candidates": [
                    {
                        "worker_id": "local", "model_revision": "m", "route_id": "route",
                        "region_id": "canada", "eligible": True, "ready": True, "loadable": True,
                        "distance_us": 10, "queue_us": 0, "preparation_us": 0,
                        "transfer_us": 0, "execution_us": 100, "uncertainty_us": 0,
                        "observation_age_us": 0, "demand_count": 1.0,
                        "demand_observed_at_us": 0
                    }
                ]
            }
        ]
    }


def test_validate_and_replay_are_offline_reproducible_and_emit_json_markdown(tmp_path):
    dataset = tmp_path / "dataset.json"
    profiles = tmp_path / "profiles.json"
    out_one = tmp_path / "one"
    out_two = tmp_path / "two"
    _write(dataset, _dataset())
    _write(profiles, {"schema_version": 1, "kind": "synthetic_profiles", "profile_id": "tiny"})

    assert _run("validate", dataset).returncode == 0
    first = _run("replay", "--dataset", dataset, "--profiles", profiles, "--policy", "nearest-ready", "--seed", 7, "--out", out_one)
    second = _run("replay", "--dataset", dataset, "--profiles", profiles, "--policy", "nearest-ready", "--seed", 7, "--out", out_two)
    assert first.returncode == second.returncode == 0
    report_one = json.loads((out_one / "report.json").read_text())
    report_two = json.loads((out_two / "report.json").read_text())
    assert report_one == report_two
    assert report_one["qualification"] == "none"
    assert "uncalibrated diagnostic replay" in (out_one / "report.md").read_text()


def test_invalid_input_and_compare_exit_codes(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version":2,"kind":"routing_dataset"}', encoding="utf-8")
    assert _run("validate", bad).returncode == 2

    benchmark = tmp_path / "benchmark.json"
    _write(
        benchmark,
        {
            "schema_version": 1,
            "kind": "comparison_benchmark",
            "calibrated": True,
            "invariants_pass": True,
            "uncertainty_invariants": [True],
            "candidates": {
                "bad": {
                    "claimed_primary": "first_output",
                    "cells": [
                        {
                            "workload_class": "tts", "requester_region": "canada", "interactive": True,
                            "incumbent_good": [True] * 40, "candidate_good": [False] * 40,
                            "incumbent_first_output_us": [100] * 40, "candidate_first_output_us": [1] * 40,
                            "incumbent_background_throughput": [], "candidate_background_throughput": []
                        }
                    ]
                }
            }
        },
    )
    out = tmp_path / "compare"
    result = _run("compare", "--benchmark", benchmark, "--candidates", "bad", "--out", out)
    assert result.returncode == 3
    assert json.loads((out / "report.json").read_text())["results"]["bad"]["status"] == "regression"


def test_observer_overhead_cli_writes_machine_and_human_reports(tmp_path):
    out = tmp_path / "overhead"
    result = _run(
        "observer-overhead",
        "--samples", 1_000,
        "--warmup", 50,
        "--out", out,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((out / "report.json").read_text())
    assert report["kind"] == "observer_overhead_report"
    assert report["status"] == "passed"
    markdown = (out / "report.md").read_text()
    assert "isolated" in markdown.lower()
    assert "live service" in markdown.lower()


def test_completeness_cli_is_read_only_and_reports_no_enabled_adapters(tmp_path):
    manifest = tmp_path / "manifest.json"
    _write(
        manifest,
        {
            "schema_version": 1,
            "kind": "adapter_trace_manifest",
            "adapters": [{
                "adapter_id": "client-a",
                "enabled": False,
                "trace_files": [],
                "arrival_events": ["start"],
                "terminal_events": ["done"],
                "known_uninstrumented_callers": ["released_client"],
            }],
        },
    )
    out = tmp_path / "completeness"
    result = _run("completeness", "--manifest", manifest, "--out", out)
    assert result.returncode == 4
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "insufficient_evidence"
    assert report["enabled_adapter_count"] == 0
    assert report["adapters"][0]["known_uninstrumented_callers"] == ["released_client"]


def test_cycle_plan_does_not_submit_or_install_weekly_trigger(tmp_path):
    config = tmp_path / "cycle.json"
    _write(config, {
        "schema_version": 1, "kind": "cycle_config", "domain_id": "routing-v1",
        "data_cutoff_utc_us": 1, "source_sha256": "a" * 64,
        "profile_sha256": "b" * 64, "dataset_sha256": "c" * 64,
        "evaluator_sha256": "d" * 64, "candidate_limit": 4,
        "revisions_per_candidate": 2, "evaluation_limit": 8, "wall_seconds": 7200,
        "cpu": 4, "memory_bytes": 8 * 1024**3, "output_bytes": 1024**3,
        "gpu_seconds": 0, "authoring_token_budget": None,
        "authoring_spend_microusd": None, "weekly_trigger_enabled": True,
    })
    out = tmp_path / "cycle-plan"
    result = _run("cycle", "plan", "--config", config, "--out", out)
    assert result.returncode == 0, result.stderr
    report = json.loads((out / "report.json").read_text())
    assert report["submission_count"] == 0
    assert report["scheduler_installed"] is False
    assert report["weekly_trigger_requested"] is True
