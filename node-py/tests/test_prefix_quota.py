"""Ceilings per owner AND per prefix.

`quota_for` answers to the longest prefix an owner lives under;
`over_quota` checks the owner's own ceiling and every enclosing prefix
ceiling independently, counting a prefix ceiling over the AGGREGATE of all
owners under it and naming that aggregate in the refusal — so `attune:` at 4
refuses `attune:acct_b`'s second job when `acct_a` holds 3, and the sentence
says "the application holds 4 of 4", never "you are over your own ceiling".
"""
from livestack_node.fleet_admit import admit
from livestack_node.fleet_scheduler import (
    SchedulerPolicy, over_quota, quota_for,
)

# The umbrella configuration.
POLICY = SchedulerPolicy(account_quotas={"attune:": 4, "attune:acct_b": 6,
                                         "benchday:": 6})

VIEW = {
    "generated_at": 1000.0,
    "hosts": {
        "h": {"nodes": [{
            "peer": "http://h:8100/livestack", "state": "fresh",
            "ready": True, "kinds": ["llm"], "detail": "resident",
            "unseen_seconds": 0.0, "load": {"in_flight": 0},
            "units": [{"kind": "llm", "resident": True}]}]},
    },
}


# -- quota_for ----------------------------------------------------------------

def test_an_exact_ceiling_beats_any_prefix():
    assert quota_for("attune:acct_b", POLICY) == 6
    assert quota_for("attune:acct_a", POLICY) == 4   # falls to the prefix


def test_the_longest_matching_prefix_wins():
    pol = SchedulerPolicy(account_quotas={"attune:": 4,
                                          "attune:acct_a:": 2})
    assert quota_for("attune:acct_a:x", pol) == 2
    assert quota_for("attune:acct_a", pol) == 4
    assert quota_for("benchday:y", pol) is None       # no ceiling answers
    assert quota_for("anyone", SchedulerPolicy()) is None


# -- over_quota: the umbrella scenario -----------------------------------------

def test_the_aggregate_ceiling_refuses_the_second_job_naming_the_aggregate():
    usage = {"attune:acct_a": 3}
    # acct_b's first job is fine: its own ceiling is 6 (holds 0), the
    # aggregate under 'attune:' is 3 of 4.
    assert over_quota("attune:acct_b", usage, POLICY) is None
    usage = dict(usage, **{"attune:acct_b": 1})
    # The second job is refused by the AGGREGATE ceiling at count 4 — and the
    # reason must not blame acct_b's own ceiling of 6, which it is nowhere near.
    reason = over_quota("attune:acct_b", usage, POLICY)
    assert reason is not None
    assert "4 of 4" in reason
    assert "aggregate over 'attune:'" in reason
    assert "6" not in reason.replace("attune:acct_b", ""), \
        "the refusal names the count in force, not the ceiling that did not fire"


def test_an_owner_at_its_own_exact_ceiling_is_refused_with_its_own_count():
    usage = {"attune:acct_b": 6}
    reason = over_quota("attune:acct_b", usage, POLICY)
    assert reason and "6 of 6" in reason and "aggregate" not in reason


def test_a_prefix_ceiling_counts_every_owner_under_it():
    pol = SchedulerPolicy(account_quotas={"attune:": 4})
    usage = {"attune:acct_a": 2, "attune:acct_b": 2}
    reason = over_quota("attune:acct_c", usage, pol)
    assert reason and "4 of 4" in reason and "aggregate" in reason


def test_no_ceiling_anywhere_refuses_nothing():
    assert over_quota("anyone", {"anyone": 99}, SchedulerPolicy()) is None


# -- through admit(): the refusal stays a 429 naming the count ------------------

def test_admit_refuses_with_429_naming_the_aggregate_count():
    policy = SchedulerPolicy(account_quotas={"attune:": 4})
    first = admit(VIEW, kind="llm", owner="attune:acct_a",
                  usage={"attune:acct_a": 3}, policy=policy, now=1000.0)
    assert first["granted"] is True, first["reason"]
    second = admit(VIEW, kind="llm", owner="attune:acct_b",
                   usage={"attune:acct_a": 3, "attune:acct_b": 1},
                   policy=policy, now=1000.0)
    assert second["granted"] is False
    assert second["refused"] == "account_quota"
    assert "4 of 4" in second["reason"]
    assert "aggregate over 'attune:'" in second["reason"]


# -- E.2: GET /fleet reports both ceilings and per-prefix usage ----------------

def test_get_fleet_reports_exact_and_prefix_ceilings_and_prefix_usage():
    from livestack_node.hostbroker import HostBroker

    broker = HostBroker(devices=[], peers=[], clock=lambda: 1000.0)
    broker.fleet_policy = SchedulerPolicy(
        account_quotas={"attune:": 4, "attune:acct_a": 2})
    quota = broker.fleet_view()["quota"]
    # Exact ceilings, unchanged.
    assert quota["account_quotas"] == {"attune:": 4, "attune:acct_a": 2}
    # Prefix ceilings in force, split out so a reader need not parse keys.
    assert quota["prefix_quotas"] == {"attune:": 4}
    # Per-prefix usage over the broker's own lease ledger (empty here).
    assert quota["prefix_usage"] == {"attune:": 0}
