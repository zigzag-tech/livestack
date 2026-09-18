import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.heldout import build_heldout_dataset, evaluate_heldout_replay


def _dataset():
    return {
        "schema_version": 1,
        "kind": "heldout_replay_dataset",
        "domain_id": "two-region-v1",
        "requests": [
            {
                "request_id": "parent",
                "workflow_id": "w",
                "arrival": {"kind": "external", "relative_time_us": 10},
                "observed_external_occupancy": 1,
                "observed_completion_us": 110,
                "simulated_completion_us": 120,
                "observed_state": "warm",
                "simulated_state": "warm",
            },
            {
                "request_id": "child",
                "workflow_id": "w",
                "arrival": {
                    "kind": "after_dependencies",
                    "dependency_request_ids": ["parent"],
                    "think_time_us": 5,
                },
                "observed_external_occupancy": 2,
                "observed_completion_us": 220,
                "simulated_completion_us": 240,
                "observed_state": "ready",
                "simulated_state": "cold",
            },
        ],
    }


def test_heldout_replay_uses_simulated_dependency_completion_and_attributes_mismatch():
    report = evaluate_heldout_replay(_dataset())
    assert report["status"] == "evaluated"
    assert report["requests"][1]["simulated_arrival_us"] == 125
    assert report["requests"][1]["observed_external_occupancy"] == 2
    assert report["mismatch_attribution"]["state_transition"] == 1
    assert report["mismatch_attribution"]["completion_timing"] == 2
    assert report["completion_error_distribution_us"] == {
        "minimum": 10, "p50": 10, "p95": 20, "maximum": 20,
        "underpredicted": 0, "overpredicted": 2, "exact": 0,
    }


@pytest.mark.parametrize("field", ["historical_queue_wait_us", "recorded_future_queue_length"])
def test_heldout_replay_rejects_historical_wait_or_future_queue_as_prediction(field):
    dataset = _dataset()
    dataset["requests"][0][field] = 10
    with pytest.raises(ContractError, match="forbidden historical prediction field"):
        evaluate_heldout_replay(dataset)


def test_empty_heldout_replay_is_insufficient_evidence():
    dataset = _dataset()
    dataset["requests"] = []
    report = evaluate_heldout_replay(dataset)
    assert report["status"] == "insufficient_evidence"


def test_heldout_dataset_uses_frozen_component_medians_not_observed_waits():
    observations = {
        "schema_version": 1, "kind": "heldout_observation_pack", "episode_count": 1,
        "contains_predictions": False,
        "requests": [
            {"request_id": "e-llm", "workflow_id": "e", "workload_class": "llm-27b",
             "arrival": {"kind": "external", "relative_time_us": 0},
             "observed_completion_us": 130, "observed_external_occupancy": 0,
             "observed_state": "resident_warm"},
            {"request_id": "e-tts", "workflow_id": "e", "workload_class": "tts",
             "arrival": {"kind": "after_dependencies", "dependency_request_ids": ["e-llm"], "think_time_us": 5},
             "observed_completion_us": 260, "observed_external_occupancy": 0,
             "observed_state": "resident_warm"},
        ],
    }
    packs = [
        {"kind": "measured_profile_pack", "cell": {"workload_class": "llm-27b"},
         "samples": [{"completion_us": 100}, {"completion_us": 120}]},
        {"kind": "measured_profile_pack", "cell": {"workload_class": "tts"},
         "samples": [{"completion_us": 50}, {"completion_us": 70}]},
    ]
    dataset = build_heldout_dataset(observations, packs, domain_id="d")
    assert dataset["requests"][0]["simulated_completion_us"] == 110
    assert dataset["requests"][1]["simulated_completion_us"] == 175
    assert "observed_start_us" not in dataset["requests"][1]
