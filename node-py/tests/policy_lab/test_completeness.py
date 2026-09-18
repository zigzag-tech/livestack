import json

from livestack_node.policy_lab.completeness import build_completeness_report


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_completeness_names_gaps_missing_lifecycle_and_uninstrumented_callers(tmp_path):
    trace = tmp_path / "speech.jsonl"
    _write_jsonl(
        trace,
        [
            {"kind": "request_arrived", "request_id": "r1"},
            {"kind": "request_arrived", "request_id": "r2"},
            {"kind": "request_terminal", "request_id": "r1", "outcome": "completed"},
            {"kind": "evidence_gap", "dropped_count": 3},
        ],
    )
    report = build_completeness_report(
        {
            "schema_version": 1,
            "kind": "adapter_trace_manifest",
            "adapters": [
                {
                    "adapter_id": "speech-a",
                    "enabled": True,
                    "trace_files": [str(trace)],
                    "arrival_events": ["request_arrived"],
                    "terminal_events": ["request_terminal"],
                    "known_uninstrumented_callers": ["legacy_batch"],
                },
                {
                    "adapter_id": "speech-b",
                    "enabled": False,
                    "trace_files": [],
                    "arrival_events": ["request_arrived"],
                    "terminal_events": ["request_terminal"],
                    "known_uninstrumented_callers": ["released_client"],
                },
            ],
        }
    )
    first, second = report["adapters"]
    assert report["status"] == "insufficient_evidence"
    assert first["dropped_observation_events"] == 3
    assert first["missing_terminal_request_ids"] == ["r2"]
    assert first["known_uninstrumented_callers"] == ["legacy_batch"]
    assert first["complete"] is False
    assert second["status"] == "disabled"
    assert second["known_uninstrumented_callers"] == ["released_client"]


def test_complete_enabled_trace_can_pass_without_extrapolating_disabled_adapter(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write_jsonl(
        trace,
        [
            {"kind": "start", "request_id": "r"},
            {"kind": "done", "request_id": "r"},
        ],
    )
    report = build_completeness_report(
        {
            "schema_version": 1,
            "kind": "adapter_trace_manifest",
            "adapters": [
                {
                    "adapter_id": "enabled",
                    "enabled": True,
                    "trace_files": [str(trace)],
                    "arrival_events": ["start"],
                    "terminal_events": ["done"],
                    "known_uninstrumented_callers": [],
                },
                {
                    "adapter_id": "disabled",
                    "enabled": False,
                    "trace_files": [],
                    "arrival_events": ["start"],
                    "terminal_events": ["done"],
                    "known_uninstrumented_callers": ["not_enabled"],
                },
            ],
        }
    )
    assert report["status"] == "complete_for_enabled_adapters"
    assert report["enabled_adapter_count"] == 1
    assert report["disabled_adapter_count"] == 1
