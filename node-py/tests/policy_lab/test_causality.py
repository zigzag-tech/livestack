import pytest

from livestack_node.policy_lab.causality import (
    CausalityError,
    derive_interval,
    resolve_arrival_us,
    validate_dependency_identity,
)


def _event(*, emitter="a", boot="boot", mono=100, utc=1_000, uncertainty=10):
    return {
        "emitter_id": emitter,
        "emitter_boot_id": boot,
        "monotonic_time_us": mono,
        "wall_time_utc_us": utc,
        "clock_uncertainty_us": uncertainty,
    }


def test_same_boot_interval_uses_monotonic_clock_exactly():
    result = derive_interval(_event(mono=100, utc=5000), _event(mono=175, utc=1))
    assert result.status == "valid"
    assert (result.lower_us, result.upper_us) == (75, 75)
    assert result.calibration_eligible


def test_cross_host_negative_uncertain_interval_is_not_manufactured():
    start = _event(emitter="a", utc=1_000, uncertainty=100)
    end = _event(emitter="b", utc=950, uncertainty=100)
    result = derive_interval(start, end)
    assert result.status == "uncertain"
    assert result.lower_us == 0
    assert result.upper_us == 150
    assert not result.calibration_eligible

    impossible = derive_interval(start, _event(emitter="b", utc=700, uncertainty=100))
    assert impossible.status == "invalid"
    assert impossible.lower_us is None
    assert not impossible.calibration_eligible


def test_negative_same_clock_duration_is_invalid_not_clamped():
    result = derive_interval(_event(mono=100), _event(mono=99))
    assert result.status == "invalid"
    assert result.lower_us is None


def test_dependency_arrival_uses_simulated_completion_plus_think_time():
    descriptor = {
        "request_id": "child",
        "workflow_id": "flow",
        "arrival": {
            "kind": "after_dependencies",
            "dependency_request_ids": ["parent-a", "parent-b"],
            "think_time_us": 25,
        },
    }
    assert resolve_arrival_us(descriptor, {"parent-a": 100, "parent-b": 150}) == 175
    with pytest.raises(CausalityError, match="missing simulated completion"):
        resolve_arrival_us(descriptor, {"parent-a": 100})


def test_workflow_dependencies_cannot_be_split_across_identities():
    requests = {
        "parent": {"request_id": "parent", "workflow_id": "one"},
        "child": {
            "request_id": "child",
            "workflow_id": "two",
            "arrival": {
                "kind": "after_dependencies",
                "dependency_request_ids": ["parent"],
                "think_time_us": 0,
            },
        },
    }
    with pytest.raises(CausalityError, match="workflow identity"):
        validate_dependency_identity(requests)
