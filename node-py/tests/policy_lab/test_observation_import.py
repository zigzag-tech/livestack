from livestack_node.policy_lab.importer import import_observations


def _event(event_id, event_type, request_id, sequence, payload=None, attempt=None):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "request_id": request_id,
        "attempt_id": attempt,
        "decision_id": None,
        "parent_request_id": None,
        "workflow_id": None,
        "emitter_id": "source",
        "emitter_boot_id": "boot",
        "sequence": sequence,
        "wall_time_utc_us": 100 + sequence,
        "monotonic_time_us": sequence,
        "clock_uncertainty_us": 0,
        "observed_at_utc_us": 200 + sequence,
        "payload": payload or {},
    }


def test_offered_denominator_retains_refused_timeout_and_unknown_work():
    events = [
        _event("a1", "request_arrived", "completed", 1, {"sampling_probability": 1.0}),
        _event("a2", "request_terminal", "completed", 2, {"outcome": "completed"}),
        _event("b1", "request_arrived", "refused", 3),
        _event("b2", "admission_refused", "refused", 4, {"reason_code": "capacity"}),
        _event("c1", "request_arrived", "expired", 5),
        _event("c2", "request_terminal", "expired", 6, {"outcome": "expired"}),
        _event("d1", "request_arrived", "unknown", 7, {"sampling_probability": 0.5}),
    ]
    dataset = import_observations(reversed(events), observation_horizon_utc_us=10_000)
    assert dataset.report.offered_logical_requests == 4
    assert dataset.report.completed == 1
    assert dataset.report.refused == 1
    assert dataset.report.expired == 1
    assert dataset.report.unknown_outcomes == 1
    assert dataset.report.attempts == 0
    assert dataset.report.sampling_probabilities == (0.5, 1.0)
    assert {row.request_id for row in dataset.requests} == {
        "completed",
        "refused",
        "expired",
        "unknown",
    }


def test_transport_attempts_do_not_enlarge_offered_denominator():
    events = [_event("arrival", "request_arrived", "logical", 1)]
    for index in range(3):
        events.append(
            _event(
                f"attempt-{index}",
                "attempt_failed",
                "logical",
                index + 2,
                {"failure_class": "transport"},
                attempt=f"physical-{index}",
            )
        )
    dataset = import_observations(events, observation_horizon_utc_us=10_000)
    assert dataset.report.offered_logical_requests == 1
    assert dataset.report.attempts == 3
    assert dataset.report.unknown_outcomes == 1


def test_gap_records_block_complete_coverage_claim():
    dataset = import_observations(
        [_event("arrival", "request_arrived", "logical", 1)],
        observation_horizon_utc_us=10_000,
        gap_summaries=[{"dropped_count": 7, "first_us": 1, "last_us": 9}],
    )
    assert dataset.report.dropped_observation_events == 7
    assert not dataset.report.complete_coverage
