"""Tests for the durable provisioning lifecycle.

The invariant every one of these exists to protect: **one logical operation
results in at most one billed create.** Everything else — the state machine, the
atomic claim, the correlated announce, the drain gate — is machinery in service
of it, so the assertions are written against the fake provider's bill rather
than against internal bookkeeping wherever they can be.
"""
import json
import threading

import pytest

from livestack_node.fleet_operations import (
    ANNOUNCED, CREATED, CREATING, FAILED, INTENT, REJECTED, RELEASED, UNCERTAIN,
    ClaimRefused, DrainRefused, IllegalTransition, LEGAL_TRANSITIONS,
    OperationStore, TERMINAL_STATES, UnknownOperation, structured_error,
)
from livestack_node.fleet_scheduler import SchedulerPolicy
from livestack_node.ledger import JsonlLedger, validate


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


def store(tmp_path, clock=None, ledger=None, **kw):
    return OperationStore(str(tmp_path / "ops.sqlite3"), clock=clock or Clock(),
                          ledger=ledger, **kw)


def claim(st, **kw):
    base = dict(job_id="j1", owner="acct_a", target_id="pool-a", kind="llm",
                idempotency_key="key-1", provider="fake", tier="SPOT",
                region="cn")
    base.update(kw)
    return st.claim(**base)


# --- the state machine ------------------------------------------------------
def test_every_legal_transition_is_reachable_and_illegal_ones_refuse(tmp_path):
    """Both halves in one test on purpose: a machine that permits everything and
    a machine that permits nothing both pass a one-sided version of this."""
    st = store(tmp_path)
    op = claim(st)
    assert op.state == CREATING          # intent -> creating, via the claim

    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    with pytest.raises(IllegalTransition) as e:
        st.transition(op.operation_id, CREATING)
    assert "created -> creating" in str(e.value)

    st.transition(op.operation_id, ANNOUNCED)
    assert st.get(op.operation_id).state == ANNOUNCED
    # A terminal state is terminal: nothing leaves `rejected` or `released`.
    for terminal in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset()


def test_an_unknown_operation_raises_rather_than_returning_none(tmp_path):
    with pytest.raises(UnknownOperation):
        store(tmp_path).transition("NOPE", CREATED)


def test_the_store_survives_a_reopen(tmp_path):
    """Durable means durable: a new process reads the same rows."""
    clock = Clock()
    op = claim(store(tmp_path, clock))
    again = store(tmp_path, clock).get(op.operation_id)
    assert again is not None and again.state == CREATING
    assert again.idempotency_key == "key-1"


# --- the claim --------------------------------------------------------------
def test_quota_boundary_refuses_the_second_claim_and_says_which_ceiling(tmp_path):
    st = store(tmp_path)
    pol = SchedulerPolicy(account_quotas={"acct_a": 1})
    claim(st, policy=pol, usage={})
    with pytest.raises(ClaimRefused) as e:
        claim(st, idempotency_key="key-2", policy=pol, usage={})
    assert "acct_a holds 1 of 1" in str(e.value)
    # The refusal is DURABLE and terminal, not an exception that left no trace.
    refused = [o for o in st.for_job("j1") if o.state == REJECTED]
    assert len(refused) == 1 and refused[0].reason


def test_a_pending_create_counts_against_quota_before_it_announces(tmp_path):
    """The gap a naive check leaves: an owner at its ceiling asks four more
    times before any of the four machines exist."""
    st = store(tmp_path)
    pol = SchedulerPolicy(account_quotas={"acct_a": 2})
    claim(st, idempotency_key="k1", policy=pol, usage={})
    # One lease already held, plus the create in flight -> at the ceiling.
    with pytest.raises(ClaimRefused):
        claim(st, idempotency_key="k2", policy=pol, usage={"acct_a": 1})


def test_a_prefix_ceiling_refuses_and_names_the_aggregate(tmp_path):
    st = store(tmp_path)
    pol = SchedulerPolicy(account_quotas={"attune:": 1})
    claim(st, owner="attune:a", idempotency_key="k1", policy=pol, usage={})
    with pytest.raises(ClaimRefused) as e:
        claim(st, owner="attune:b", idempotency_key="k2", policy=pol, usage={})
    assert "aggregate over 'attune:'" in str(e.value)


def test_concurrent_claims_at_the_boundary_produce_exactly_one_winner(tmp_path):
    """The reason the check is inside one writer transaction. Run without the
    lock this test passes twice."""
    st = store(tmp_path)
    pol = SchedulerPolicy(account_quotas={"acct_a": 1})
    wins, refusals = [], []
    start = threading.Barrier(8)

    def go(i):
        start.wait()
        try:
            wins.append(claim(st, idempotency_key=f"k{i}", policy=pol, usage={}))
        except ClaimRefused as e:
            refusals.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1, [w.operation_id for w in wins]
    assert len(refusals) == 7


def test_a_region_outside_the_caller_policy_is_refused(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ClaimRefused) as e:
        claim(st, region="cn", allow_regions=("na",))
    assert "caller allows na" in str(e.value)


def test_an_unknown_region_is_excluded_unless_the_caller_allows_it(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ClaimRefused):
        claim(st, region=None, allow_regions=("na",))
    op = claim(st, idempotency_key="k2", region=None, allow_regions=("na",),
               allow_unknown_region=True)
    assert op.state == CREATING


def test_the_same_idempotency_key_returns_the_same_operation(tmp_path):
    st = store(tmp_path)
    a = claim(st)
    b = claim(st)
    assert a.operation_id == b.operation_id
    assert len(st.for_job("j1")) == 1


def test_replaying_a_refused_key_refuses_again_rather_than_claiming(tmp_path):
    st = store(tmp_path)
    pol = SchedulerPolicy(account_quotas={"acct_a": 0})
    with pytest.raises(ClaimRefused):
        claim(st, policy=pol, usage={})
    with pytest.raises(ClaimRefused):
        claim(st, policy=pol, usage={})
    assert len(st.for_job("j1")) == 1


# --- restart and reconciliation --------------------------------------------
def test_restart_mid_create_marks_uncertain_and_holds_the_quota(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock)
    op = claim(st)
    # A new process over the same file: nothing is in memory, everything is on disk.
    st2 = store(tmp_path, clock)
    recovered = st2.recover()
    assert [o.operation_id for o in recovered] == [op.operation_id]
    assert st2.get(op.operation_id).state == UNCERTAIN
    # Still counted: an unresolved create is capacity the owner has spent.
    assert st2.pending_usage() == {"acct_a": 1}


def test_reconcile_resolves_to_created_when_the_provider_has_the_instance(tmp_path):
    st = store(tmp_path)
    op = claim(st)
    st.transition(op.operation_id, UNCERTAIN, reason="reply lost")
    out = st.reconcile(op.operation_id, lambda key: "i-9" if key == "key-1" else None)
    assert out.state == CREATED and out.provider_instance_id == "i-9"


def test_reconcile_resolves_to_rejected_when_it_provably_did_not(tmp_path):
    st = store(tmp_path)
    op = claim(st)
    st.transition(op.operation_id, UNCERTAIN, reason="reply lost")
    out = st.reconcile(op.operation_id, lambda key: None)
    assert out.state == REJECTED and "nothing was billed" in out.reason


def test_a_lookup_that_cannot_answer_propagates_rather_than_meaning_no(tmp_path):
    """The collapse this state exists to prevent. If a failing lookup returned
    None, an unreachable provider would silently free every uncertain create."""
    st = store(tmp_path)
    op = claim(st)
    st.transition(op.operation_id, UNCERTAIN, reason="reply lost")

    def down(_key):
        raise RuntimeError("provider API unreachable")

    with pytest.raises(RuntimeError):
        st.reconcile(op.operation_id, down)
    assert st.get(op.operation_id).state == UNCERTAIN


# --- correlation ------------------------------------------------------------
def test_an_unrelated_node_becoming_fresh_does_not_green_the_operation(tmp_path):
    st = store(tmp_path)
    op = claim(st)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    assert st.announce("SOME-OTHER-OPERATION", ready=True) is None
    assert st.get(op.operation_id).state == CREATED


def test_a_correlated_node_that_is_not_ready_does_not_green_it_either(tmp_path):
    st = store(tmp_path)
    op = claim(st)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    assert st.announce(op.operation_id, ready=False).state == CREATED
    assert st.announce(op.operation_id, ready=True).state == ANNOUNCED


def test_created_but_never_announced_fails_on_its_deadline(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock)
    op = claim(st, announce_deadline_s=600.0)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    assert st.expire() == []
    clock.advance(601)
    failed = st.expire()
    assert [o.state for o in failed] == [FAILED]
    assert failed[0].error["code"] == "never_announced"


# --- draining ---------------------------------------------------------------
def announced(tmp_path, clock=None, **kw):
    st = store(tmp_path, clock or Clock(), **kw)
    op = claim(st)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    st.announce(op.operation_id, ready=True)
    return st, op


def test_a_busy_worker_is_not_released(tmp_path):
    st, op = announced(tmp_path)
    with pytest.raises(DrainRefused) as e:
        st.release(op.operation_id, busy=lambda: "1 active lease")
    assert "1 active lease" in str(e.value)
    assert st.get(op.operation_id).state == ANNOUNCED


def test_a_job_admitted_between_the_plan_and_the_act_still_stops_the_release(tmp_path):
    """The final re-check under the writer lock. The plan was computed from a
    view, and a view is always a few seconds old."""
    st, op = announced(tmp_path)
    calls = []

    def busy():
        calls.append(1)
        return None if len(calls) == 1 else "a job was admitted while draining"

    with pytest.raises(DrainRefused):
        st.release(op.operation_id, busy=busy)
    assert len(calls) == 2
    assert st.get(op.operation_id).state == ANNOUNCED
    # And the drain claim rolled back with it, so the next honest attempt is not
    # blocked by the refused one.
    assert st.drain_claimed(op.target_id) is None


def test_an_empty_worker_is_released_and_the_drain_claim_recorded(tmp_path):
    st, op = announced(tmp_path)
    out = st.release(op.operation_id, busy=lambda: None)
    assert out.state == RELEASED and out.drain_claimed_at is not None
    assert st.drain_claimed(op.target_id) is not None


# --- bounds -----------------------------------------------------------------
def test_the_bound_deletes_only_terminal_rows(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock, max_records=3)
    keep = claim(st, idempotency_key="live")          # stays `creating`
    for i in range(10):
        clock.advance(1)
        with pytest.raises(ClaimRefused):
            claim(st, idempotency_key=f"dead-{i}", region="cn",
                  allow_regions=("na",))
    assert st.get(keep.operation_id) is not None       # never evicted
    assert len(st.active()) == 1


def test_an_unset_age_window_deletes_nothing_by_age(tmp_path):
    clock = Clock()
    st = store(tmp_path, clock, max_records=1000, max_age_s=None)
    with pytest.raises(ClaimRefused):
        claim(st, idempotency_key="old", region="cn", allow_regions=("na",))
    clock.advance(86400 * 3650)
    claim(st, idempotency_key="new")
    assert len(st.for_job("j1")) == 2


# --- the retrospective ------------------------------------------------------
def test_every_transition_leaves_a_valid_ledger_record_joinable_by_job_id(tmp_path):
    led = JsonlLedger(str(tmp_path / "d.jsonl"))
    st = store(tmp_path, ledger=led)
    op = claim(st)
    st.transition(op.operation_id, CREATED, provider_instance_id="i-1")
    st.announce(op.operation_id, ready=True)
    st.release(op.operation_id, busy=lambda: None)

    records = led.read()
    for r in records:
        assert validate(r) == [], (r, validate(r))
    states = [r["outcome"]["state"] for r in records]
    assert states == [INTENT, CREATING, CREATED, ANNOUNCED, RELEASED]
    # One query, by job_id, reconstructs the whole history — which is the point.
    mine = [r for r in records if r["request"]["job_id"] == "j1"]
    assert len(mine) == len(records)
    assert {r["request"]["operation_id"] for r in mine} == {op.operation_id}
    assert mine[0]["decision"] == "claim" and mine[-1]["decision"] == "operation"


def test_a_ledger_that_cannot_write_degrades_the_operation_rather_than_the_work(tmp_path):
    class Broken(JsonlLedger):
        def append(self, decision):
            return None                 # what a full disk looks like from here

    st = store(tmp_path, ledger=Broken(str(tmp_path / "d.jsonl")))
    op = claim(st)
    assert op.state == CREATING                     # the claim still happened
    assert st.get(op.operation_id).observability_degraded is True


def test_structured_error_bounds_the_excerpt(tmp_path):
    e = structured_error("create", "provider_fault", "500", "x" * 5000)
    assert len(e["excerpt"]) == 400
    assert e["stage"] == "create" and e["class"] == "provider_fault"
