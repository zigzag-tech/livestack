from livestack_node.policy_lab.adapters.harmony import correlate_harmony_records


def test_tentative_grant_load_failure_and_actual_serving_are_distinct():
    records = [
        {"kind": "decision", "decision_id": "d1", "request_id": "r1", "chosen": "worker"},
        {"kind": "prepare_failed", "decision_id": "d1", "request_id": "r1", "attempt_id": "a1", "failure_class": "missing_weights"},
        {"kind": "decision", "decision_id": "d2", "request_id": "r2", "chosen": "worker"},
        {"kind": "first_output", "decision_id": "d2", "request_id": "r2", "attempt_id": "a2", "model_revision": "model"},
        {"kind": "execution_finished", "decision_id": "d2", "request_id": "r2", "attempt_id": "a2", "outcome": "completed"},
    ]
    correlated = correlate_harmony_records(records)
    assert correlated["r1"].tentative_grants == ("d1",)
    assert correlated["r1"].prepare_failures == ("missing_weights",)
    assert correlated["r1"].served_attempts == ()
    assert correlated["r2"].tentative_grants == ("d2",)
    assert correlated["r2"].served_attempts == ("a2",)
    assert correlated["r2"].terminal_outcome == "completed"


def test_missing_serving_event_stays_unknown():
    correlated = correlate_harmony_records(
        [{"kind": "decision", "decision_id": "d", "request_id": "r", "chosen": "worker"}]
    )
    assert correlated["r"].terminal_outcome == "unknown"
    assert correlated["r"].served_attempts == ()
