import json
from pathlib import Path

from livestack_node.policy_lab.overhead import MIN_OVERHEAD_SAMPLES


REPORT = Path(__file__).parents[2] / "docs" / "policy-lab" / "observer-overhead" / "report.json"


def test_pinned_observer_overhead_evidence_passes_all_predeclared_gates():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["kind"] == "observer_overhead_report"
    assert report["scope"] == "isolated_python_streaming_callback"
    assert report["status"] == "passed"
    assert report["measurement"]["samples"] >= MIN_OVERHEAD_SAMPLES
    assert report["added_p95_ms"] <= report["allowed_added_p95_ms"]
    assert all(report["checks"].values())
    assert report["live_service_performance_claim"] is False
