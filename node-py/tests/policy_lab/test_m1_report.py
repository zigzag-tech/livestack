import json
from pathlib import Path


REPORT = Path(__file__).parents[2] / "docs" / "policy-lab" / "m1-benchmark-report.json"


def test_m1_report_is_explicitly_synthetic_uncalibrated_and_complete():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["scenario_count"] == 32
    assert report["mechanics_status"] == "passed"
    assert report["evidence_level"] == "synthetic-only"
    assert report["qualification"] == "none"
    assert report["calibrated"] is False
    assert report["live_performance_claim"] is False
    assert len(report["policies"]) == 5
