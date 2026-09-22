import json

import pytest

from livestack_node.experiments import (
    ExperimentError, ExperimentStore, assignment, decision, load_policies, validate_policy,
)


POLICY = {
    "id": "asr-v1", "kind": "polyasr", "cohorts": ["en-live"],
    "control": "qwen", "variants": ["qwen", "r2t2"],
    "exploration_share": .1, "control_floor": .1, "min_samples": 10,
    "confidence": .95,
    "hysteresis_margin": .01,
    "guardrails": {"max_failure_rate": .03, "max_finalization_ms": 5000},
    "retention_days": 30, "max_observations": 100, "promoted": None, "paused": False,
}


def test_config_refuses_unbounded_exposure_and_storage(tmp_path):
    validate_policy(POLICY)
    with pytest.raises(ExperimentError):
        validate_policy({**POLICY, "exploration_share": .8})
    with pytest.raises(ExperimentError):
        validate_policy({**POLICY, "retention_days": 0})
    path = tmp_path / "experiments.json"
    path.write_text(json.dumps([POLICY]))
    assert load_policies(path)["asr-v1"]["control"] == "qwen"


def test_assignment_is_stable_and_bounded():
    available = {"qwen", "r2t2"}
    assert assignment(POLICY, "dictation-7", "en-live", available) == \
        assignment(POLICY, "dictation-7", "en-live", available)
    modes = [assignment(POLICY, f"d-{i}", "en-live", available)["mode"] for i in range(10000)]
    assert 700 <= modes.count("exploration") <= 1300


def test_store_rejects_accuracy_proxies_and_enforces_count(tmp_path):
    store = ExperimentStore(str(tmp_path / "obs.sqlite"), POLICY)
    with pytest.raises(ExperimentError):
        store.observe(subject="x", cohort="en-live", variant="r2t2",
                      metric="accuracy", value=.9, source="runtime")
    for i in range(140):
        store.observe(subject=f"d{i}", cohort="en-live", variant="r2t2",
                      metric="finalization_ms", value=1000+i, source="runtime", at=1000+i)
    count = store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert count == 100


def test_guardrail_blocks_a_high_accuracy_variant():
    summary = {
        "qwen": {"accuracy": {"count": 20, "mean": .8}},
        "r2t2": {"accuracy": {"count": 20, "mean": .9},
                 "failure": {"count": 20, "mean": .2}},
    }
    assert decision(POLICY, summary)["promoted"] is None
