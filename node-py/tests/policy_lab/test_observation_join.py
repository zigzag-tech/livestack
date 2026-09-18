import copy

import pytest

from livestack_node.policy_lab.observation import EventConflict, ObservationJournal


def event(event_id, event_type, *, sequence, boot="boot-a", attempt_id=None, request_id="req-1"):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "request_id": request_id,
        "attempt_id": attempt_id,
        "decision_id": None,
        "parent_request_id": None,
        "workflow_id": None,
        "emitter_id": "desktop",
        "emitter_boot_id": boot,
        "sequence": sequence,
        "wall_time_utc_us": 1_000 + sequence,
        "monotonic_time_us": 10 + sequence,
        "clock_uncertainty_us": 5,
        "observed_at_utc_us": 2_000 + sequence,
        "payload": {},
    }


def test_s20_three_transport_attempts_are_one_logical_demand():
    journal = ObservationJournal()
    events = [event("arrive", "request_arrived", sequence=1)]
    for number in range(3):
        events.extend(
            [
                event(
                    f"start-{number}",
                    "execution_started",
                    sequence=2 + number * 2,
                    attempt_id=f"attempt-{number}",
                ),
                event(
                    f"fail-{number}",
                    "attempt_failed",
                    sequence=3 + number * 2,
                    attempt_id=f"attempt-{number}",
                ),
            ]
        )
    for item in reversed(events):  # deliberately reordered ingestion
        assert journal.append(item) == "appended"

    joined = journal.joined_requests()
    assert journal.logical_demand_count == 1
    assert set(joined["req-1"].attempts) == {"attempt-0", "attempt-1", "attempt-2"}
    assert [item.event_id for item in joined["req-1"].events] == [item["event_id"] for item in events]


def test_retransmission_is_idempotent_but_conflicting_event_id_fails():
    journal = ObservationJournal()
    original = event("same", "request_arrived", sequence=1)
    assert journal.append(original) == "appended"
    assert journal.append(copy.deepcopy(original)) == "duplicate"

    changed = copy.deepcopy(original)
    changed["payload"] = {"changed": True}
    with pytest.raises(EventConflict, match="same event_id"):
        journal.append(changed)


def test_sequence_identity_is_boot_scoped_and_conflicts_within_boot():
    journal = ObservationJournal()
    journal.append(event("old", "request_arrived", sequence=1, boot="boot-old"))
    journal.append(event("new", "queued", sequence=1, boot="boot-new"))

    with pytest.raises(EventConflict, match="emitter boot sequence"):
        journal.append(event("collision", "queued", sequence=1, boot="boot-new"))


def test_attempt_identity_cannot_move_between_logical_requests():
    journal = ObservationJournal()
    journal.append(event("a", "execution_started", sequence=1, attempt_id="attempt"))
    with pytest.raises(EventConflict, match="attempt_id"):
        journal.append(
            event(
                "b",
                "attempt_failed",
                sequence=2,
                attempt_id="attempt",
                request_id="req-2",
            )
        )
