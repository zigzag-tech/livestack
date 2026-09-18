import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.heldout import evaluate_heldout_replay


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
