import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.objectives import evaluate_offered_load, parse_objective


def _objective(**changes):
    value = {
        "schema_version": 1,
        "kind": "objective_profile",
        "objective_id": "interactive",
        "priority_class": "interactive",
        "first_output_budget_us": 100,
        "completion_budget_us": 200,
        "streaming_lag_budget_us": None,
        "allowable_miss_rate": 0.01,
        "fairness_start_bound_us": None,
    }
    value.update(changes)
    return value


def test_objective_parser_is_strict_and_allows_explicit_impossible_zero_budget():
    parsed = parse_objective(_objective(first_output_budget_us=0))
    assert parsed.first_output_budget_us == 0
    with pytest.raises(ContractError, match="unknown fields"):
        parse_objective(_objective(legacy_priority=99))
    with pytest.raises(ContractError, match="allowable_miss_rate"):
        parse_objective(_objective(allowable_miss_rate=1.1))


def test_impossible_slo_and_capacity_refusal_remain_unsuccessful_offered_service():
    objective = parse_objective(_objective(first_output_budget_us=0))
    report = evaluate_offered_load(
        objective,
        [
            {
                "request_id": "completed-too-late",
                "eligible": True,
                "outcome": "completed",
                "first_output_us": 1,
                "completion_us": 2,
                "cancellation_cause": None,
            },
            {
                "request_id": "refused",
                "eligible": True,
                "outcome": "refused",
                "first_output_us": None,
                "completion_us": None,
                "cancellation_cause": None,
            },
        ],
    )
    assert report.offered_eligible == 2
    assert report.slo_good == 0
    assert report.refused == 1
    assert report.good_fraction_all == 0


def test_external_and_system_cancellation_are_explicit_and_have_sensitivity_views():
    objective = parse_objective(_objective())
    report = evaluate_offered_load(
        objective,
        [
            {
                "request_id": "user",
                "eligible": True,
                "outcome": "canceled",
                "first_output_us": None,
                "completion_us": None,
                "cancellation_cause": "external_user",
            },
            {
                "request_id": "system",
                "eligible": True,
                "outcome": "canceled",
                "first_output_us": None,
                "completion_us": None,
                "cancellation_cause": "system_wait",
            },
        ],
    )
    assert report.offered_eligible == 2
    assert report.external_user_canceled == 1
    assert report.system_canceled == 1
    assert report.denominator_excluding_external_cancels == 1
    assert report.denominator_treating_cancels_as_failures == 2
