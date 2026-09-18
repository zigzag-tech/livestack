import pytest

from livestack_node.policy_lab.request_lifecycle import LifecycleError, RequestLifecycle


def test_retries_share_one_absolute_deadline():
    request = RequestLifecycle("request", arrival_us=0, deadline_us=100)
    request.queue(0)
    request.start_attempt("attempt-a", 10)
    request.fail_attempt("attempt-a", 90, "transport")
    request.start_attempt("attempt-b", 99)
    with pytest.raises(LifecycleError, match="deadline"):
        request.mark_executing("attempt-b", 100)
    assert request.deadline_us == 100


def test_duplicate_completion_and_resource_release_are_idempotent_not_double_counted():
    request = RequestLifecycle("request", arrival_us=0, deadline_us=100)
    request.queue(0)
    request.start_attempt("attempt", 1)
    request.reserve("attempt", 2, "lease")
    request.mark_executing("attempt", 3)
    assert request.complete("attempt", 4, "result") == "completed"
    assert request.complete("attempt", 4, "result") == "duplicate"
    assert request.release_after_cleanup("attempt", "lease") == "released"
    assert request.release_after_cleanup("attempt", "lease") == "duplicate"
    assert request.resource_release_count == 1
    with pytest.raises(LifecycleError, match="conflicting completion"):
        request.complete("attempt", 4, "other-result")


def test_cancel_terminal_state_does_not_imply_cleanup():
    request = RequestLifecycle("request", arrival_us=0, deadline_us=None)
    request.queue(0)
    request.start_attempt("attempt", 1)
    request.reserve("attempt", 2, "lease")
    request.mark_executing("attempt", 3)
    request.request_cancel(4, cause="external_user")
    request.acknowledge_cancel("attempt", 5)
    assert request.state == "terminal"
    assert request.attempts["attempt"].lease_id == "lease"
    assert not request.attempts["attempt"].resource_released
