from livestack_node.policy_lab.metrics import (
    MetricRecord,
    aggregate_cells,
    paired_mean_ci,
)


def test_metrics_are_reported_by_class_region_principal_and_application():
    rows = [
        MetricRecord("a", "tts", "canada", "p1", "desktop", True, True, 100),
        MetricRecord("b", "tts", "canada", "p2", "benchday", True, False, 200),
        MetricRecord("c", "asr", "china", "p1", "benchday", True, True, 50),
    ]
    cells = aggregate_cells(rows)
    assert set(cells) == {
        ("tts", "canada", "p1", "desktop"),
        ("tts", "canada", "p2", "benchday"),
        ("asr", "china", "p1", "benchday"),
    }
    assert cells[("tts", "canada", "p2", "benchday")].offered == 1


def test_paired_confidence_detects_improvement_regression_and_insufficient_power():
    improvement = paired_mean_ci([(10.0, 8.0)] * 40, resamples=1000, seed=7)
    regression = paired_mean_ci([(10.0, 12.0)] * 40, resamples=1000, seed=7)
    insufficient = paired_mean_ci([(10.0, 8.0)] * 5, resamples=1000, seed=7)
    assert improvement.status == "sufficient" and improvement.upper < 0
    assert regression.status == "sufficient" and regression.lower > 0
    assert insufficient.status == "insufficient_evidence"
