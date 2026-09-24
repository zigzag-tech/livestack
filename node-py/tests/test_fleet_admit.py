"""Job admission: throw a task at the fleet and it knows where to run it.

Pure over a fleet view, so every case is answered exactly rather than guessed at
from a live run. What the cases are mostly about is the SHAPE of the answer: the
grant is one line, and the record beside it has to be enough for a future agent
to say "it should have gone to X" — or to check that it should not have.
"""
from livestack_node.fleet_admit import DEFAULT_CONCURRENCY, admit, targets_from_view
from livestack_node.fleet_scheduler import SchedulerPolicy, Weights
from livestack_node.ledger import validate, Decision


def _node(peer, host, *, state="fresh", ready=True, probe_ms=None, in_flight=None,
          pressure=None, kinds=("polyasr",), units=None, device_id=None,
          detail="resident", last_error=None, labels=None):
    n = {"peer": peer, "state": state, "ready": ready, "kinds": list(kinds),
         "detail": detail, "unseen_seconds": 0.0, "device_id": device_id or f"{host}/dev"}
    if probe_ms is not None:
        n["probe_ms"] = probe_ms
    if in_flight is not None or pressure is not None:
        n["load"] = {k: v for k, v in
                     (("in_flight", in_flight), ("pressure", pressure)) if v is not None}
    if units is not None:
        n["units"] = units
    if last_error:
        n["last_error"] = last_error
    if labels:
        n["labels"] = labels
    return n


def _view(hosts, links=None, generated_at=1000.0):
    out = {"generated_at": generated_at, "hosts": {}}
    for host, nodes in hosts.items():
        out["hosts"][host] = {"nodes": nodes}
        if links and host in links:
            out["hosts"][host]["links"] = links[host]
    return out


ALIGN_RESIDENT = [{"kind": "align", "resident": True}]
ALIGN_COLD = [{"kind": "align", "resident": False}]

# The shape §5.4 verification 1 describes: Nanjing's card at 0.8 pressure and
# three requests deep, Toronto's at 0.2 and idle.
BUSY_CN_IDLE_NA = _view(
    {
        "zz-tower0": [_node("http://100.64.0.3:8766/livestack", "zz-tower0",
                            probe_ms=527.0, in_flight=3, pressure=0.81,
                            units=ALIGN_COLD)],
        "xc-tower-ubuntu": [_node("http://100.64.0.18:8766/livestack", "xc-tower-ubuntu",
                                  probe_ms=2.1, in_flight=0, pressure=0.21,
                                  units=ALIGN_RESIDENT)],
    },
    links={"zz-tower0": {"xc-tower-ubuntu": 605.0},
           "xc-tower-ubuntu": {"zz-tower0": 1554.0}},
)


def _row(result, target_id):
    return next(c for c in result["candidates"] if c.id == target_id)


# -- the operator's example, made mechanical ---------------------------------

def test_a_batch_job_lands_on_the_idle_card_and_the_reason_names_the_busy_one():
    """§5.4 verification 1. The reason is the deliverable as much as the grant:
    "it should have gone to X" is a question about ONE alternative, so the
    sentence has to name one and say what it lost on."""
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="media-corpus",
              now=1000.0)
    assert r["granted"] is True
    assert r["target"]["host_id"] == "xc-tower-ubuntu"
    assert r["target"]["node"] == "http://100.64.0.18:8766/livestack"
    assert "http://100.64.0.3:8766 lost on" in r["reason"]
    assert "pressure=0.81" in r["reason"]
    assert "in_flight=3" in r["reason"]


def test_locality_and_distance_bring_an_interactive_job_home():
    """§5.4 verification 2. From a Nanjing caller, distance dominates within
    feasibility — the same fleet, the same instant, the opposite answer."""
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive", estimate_s=5.0,
              locality_host="zz-tower0", vantage="host:zz-tower0", now=1000.0)
    assert r["granted"] is True
    assert r["target"]["host_id"] == "zz-tower0", r["reason"]
    assert _row(r, "http://100.64.0.3:8766").distance_ms == 0.0


def test_the_same_request_from_two_vantages_gets_two_answers():
    near = admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive", estimate_s=5.0,
                 now=1000.0)
    far = admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive", estimate_s=5.0,
                vantage="host:zz-tower0", locality_host="zz-tower0", now=1000.0)
    assert near["target"]["host_id"] != far["target"]["host_id"]


# -- what may be a candidate at all ------------------------------------------

def test_a_node_that_is_not_fresh_or_not_ready_is_a_filtered_row():
    view = _view({"h": [
        _node("http://ok/livestack", "h", probe_ms=2.0, in_flight=0, units=ALIGN_RESIDENT),
        _node("http://gone/livestack", "h", state="suspect", probe_ms=2.0,
              last_error="Connection refused", units=ALIGN_COLD),
        _node("http://cold/livestack", "h", ready=False, probe_ms=2.0,
              detail="no unit resident", units=ALIGN_COLD),
        _node("http://tts/livestack", "h", kinds=("polytts",), probe_ms=2.0),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["target"]["target_id"] == "http://ok"
    filtered = {c.id: c.reason for c in r["candidates"] if c.outcome == "filtered"}
    assert "state=suspect" in filtered["http://gone"]
    assert "Connection refused" in filtered["http://gone"]
    # Cold is a candidate, not a filtered row -- it just loses to a warm node.
    assert "http://cold" not in filtered
    assert "does not host align" in filtered["http://tts"]


def test_a_cold_node_is_chosen_when_it_is_the_only_one():
    # Residency is the host's decision, reached through the caller's request.
    # A healthy node evicted for another kind on a shared card must still be
    # routable, or it is never reloaded (attune TTS/LLM, 2026-09-23).
    view = _view({"h": [
        _node("http://cold/livestack", "h", ready=False, probe_ms=2.0,
              detail="no unit resident", units=ALIGN_COLD),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["target"]["target_id"] == "http://cold"


def test_an_unready_node_that_failed_its_probe_is_still_filtered():
    view = _view({"h": [
        _node("http://broken/livestack", "h", ready=False, probe_ms=2.0,
              detail="readiness probe failed: boom", units=ALIGN_COLD),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["target"] is None
    filtered = {c.id: c.reason for c in r["candidates"] if c.outcome == "filtered"}
    assert "not ready" in filtered["http://broken"]


def test_a_saturated_node_is_filtered_with_the_number_that_saturated_it():
    view = _view({"h": [
        _node("http://full/livestack", "h", probe_ms=2.0,
              in_flight=int(DEFAULT_CONCURRENCY), units=ALIGN_RESIDENT),
        _node("http://free/livestack", "h", probe_ms=400.0, in_flight=0,
              units=ALIGN_RESIDENT),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["target"]["target_id"] == "http://free"
    assert "saturated (in_flight=4 of 4)" in _row(r, "http://full").reason


def test_a_silent_node_is_credited_with_capacity_and_the_record_says_so():
    """Refusing to schedule anything that has not reported is how a fleet
    strands its quietest engines. The uncertainty goes in the reason, where a
    reader can see the choice was made, rather than into the number."""
    view = _view({"h": [_node("http://quiet/livestack", "h", probe_ms=2.0,
                              units=ALIGN_RESIDENT)]})
    r = admit(view, kind="align", now=1000.0)
    assert r["granted"] is True
    assert "no opinion, credited 4 slot(s)" in _row(r, "http://quiet").reason


def test_nothing_feasible_is_an_answer_with_a_reason_not_a_silence():
    view = _view({"h": [_node("http://gone/livestack", "h", state="mia",
                              units=ALIGN_COLD)]})
    r = admit(view, kind="align", now=1000.0)
    assert r["granted"] is False
    assert r["target"] is None
    assert "no fleet target can run align" in r["reason"]
    assert "1 candidate(s) filtered" in r["reason"]
    assert len(r["candidates"]) == 1, "the eliminated node is still a row"


def test_a_selector_is_honoured_against_the_nodes_own_labels():
    view = _view({"h": [
        _node("http://cuda/livestack", "h", probe_ms=400.0, in_flight=0,
              units=ALIGN_RESIDENT, labels={"arch": "cuda"}),
        _node("http://mlx/livestack", "h", probe_ms=2.0, in_flight=0,
              units=ALIGN_RESIDENT, labels={"arch": "mlx"}),
    ]})
    r = admit(view, kind="align", selector={"arch": "cuda"}, now=1000.0)
    assert r["target"]["target_id"] == "http://cuda", r["reason"]


# -- distance as a scheduler term --------------------------------------------

def test_an_unmeasured_distance_scores_as_the_worst_measured_one():
    """Not zero — that would let a target win by never having been probed. Not
    infinity — that would exile it even when it is the only one left."""
    view = _view({"h": [
        _node("http://near/livestack", "h", probe_ms=2.0, in_flight=0, units=ALIGN_RESIDENT),
        _node("http://unmeasured/livestack", "h", in_flight=0, units=ALIGN_RESIDENT),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["target"]["target_id"] == "http://near"
    assert _row(r, "http://unmeasured").distance_band == "unknown"

    # And when it is the only candidate, it still wins.
    only = _view({"h": [_node("http://unmeasured/livestack", "h", in_flight=0,
                              units=ALIGN_RESIDENT)]})
    assert admit(only, kind="align", now=1000.0)["granted"] is True


def test_zero_distance_weight_restores_the_pre_distance_behaviour():
    """The term is additive and can be turned off, which is what makes it safe
    to add to a scheduler that already worked."""
    off = SchedulerPolicy(weights=Weights(distance=0.0))
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", policy=off, now=1000.0)
    # Load still decides here (the far node has 1 free slot to the near node's 4),
    # so the winner is the same — but it is now decided WITHOUT distance.
    assert r["granted"] is True


def test_a_fleet_with_no_measurements_at_all_still_schedules():
    view = _view({"h": [
        _node("http://a/livestack", "h", in_flight=0, units=ALIGN_RESIDENT),
        _node("http://b/livestack", "h", in_flight=0, units=ALIGN_RESIDENT),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert r["granted"] is True


# -- the record --------------------------------------------------------------

def test_every_candidate_row_carries_an_outcome_and_a_reason():
    view = _view({"h": [
        _node("http://ok/livestack", "h", probe_ms=2.0, in_flight=0, units=ALIGN_RESIDENT),
        _node("http://also/livestack", "h", probe_ms=9.0, in_flight=1, units=ALIGN_COLD),
        _node("http://gone/livestack", "h", state="mia", units=ALIGN_COLD),
    ]})
    r = admit(view, kind="align", now=1000.0)
    assert len(r["candidates"]) == 3
    assert sum(1 for c in r["candidates"] if c.outcome == "chosen") == 1
    assert sum(1 for c in r["candidates"] if c.outcome == "ranked") == 1
    assert sum(1 for c in r["candidates"] if c.outcome == "filtered") == 1
    for c in r["candidates"]:
        assert c.reason


def test_the_record_says_whether_the_unit_was_already_resident():
    """Whether a job needs a model LOADED is most of what it costs, so a
    retrospective that cannot see it cannot judge the placement."""
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", now=1000.0)
    assert _row(r, "http://100.64.0.18:8766").resident is True
    assert _row(r, "http://100.64.0.3:8766").resident is False


def test_an_admit_becomes_a_valid_ledger_record():
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="media-corpus",
              now=1000.0)
    d = Decision(
        emitter="fleet-broker", emitter_id="xc-tower-ubuntu:8801",
        kind=r["kind"], decision="admit", candidates=list(r["candidates"]),
        chosen=r["target"]["target_id"], reason=r["reason"],
        request={"owner": "media-corpus", "sla": "batch",
                 "vantage": r["vantage"], "selector": {}, "locality_host": None},
    )
    assert validate(d.to_dict()) == []


def test_targets_carry_the_device_id_so_a_grant_can_name_the_card():
    targets, _rows = targets_from_view(BUSY_CN_IDLE_NA, "align")
    assert {t.labels["device_id"] for t in targets} == {
        "zz-tower0/dev", "xc-tower-ubuntu/dev"}
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", now=1000.0)
    assert r["target"]["device_id"] == "xc-tower-ubuntu/dev"


def test_an_interactive_job_is_admitted_to_a_node_that_has_room_now():
    """An SLA deadline gates when a job may START, not when it must have finished.

    This test used to assert the opposite, and pinned a bug as behaviour. Its own
    docstring said the refusal "reads like a bug until you see which term produced
    it" — it read like one because it was one: `_eta` added `est_duration_s`, so
    INTERACTIVE's 30 s slack meant "must COMPLETE within 30 s", and `admit`'s own
    default 60 s estimate was therefore infeasible on a completely idle fleet. Every
    interactive caller that did not state an estimate was refused with *no feasible
    target meets the deadline now* — indistinguishable from a full fleet.

    Nothing on this fleet sent `interactive` yet (attune, media-corpus and
    `lease_helper` all send `batch`), which is why it was never reported.
    """
    for estimate in (None, 8.0, 60.0, 600.0):
        kw = {} if estimate is None else {"estimate_s": estimate}
        r = admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive", now=1000.0, **kw)
        assert r["granted"] is True, (estimate, r["reason"])
    # And the default is the case that mattered: a caller that states no estimate
    # at all must not be refused by a fleet with room.
    assert admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive",
                 now=1000.0)["granted"] is True


def test_an_interactive_job_still_refuses_a_target_it_cannot_reach_in_time():
    """The promise still binds — it binds on WAITING, which is the thing an SLA
    class is about. A target that cannot take the job for longer than the slack is
    infeasible however fast the job itself would be."""
    from livestack_node.fleet_scheduler import (
        CostModel, FleetState, Job, Queue, Sla, Target, Tier, schedule)
    cold = Target(id="pool", host_id="pool", tier=Tier.SPOT,
                  capacity={"concurrency": 4.0}, cost=CostModel(per_hour=2.0),
                  provision_latency_s=240.0, running=False, elastic=True,
                  max_instances=2)
    def plan_for(sla):
        job = Job(id="j", kind="align", need={"concurrency": 1.0}, owner="o",
                  created_at=1000.0, sla=sla, est_duration_s=8.0)
        return schedule(FleetState(targets=(cold,), jobs=(job,), now=1000.0))
    # 240 s of provisioning against 30 s of interactive slack: never.
    assert [type(a) for a in plan_for(Sla.INTERACTIVE).actions] == [Queue]
    # The same cold pool is fine for work that can wait.
    assert [type(a) for a in plan_for(Sla.BATCH).actions] != [Queue]


def test_dropping_the_runtime_from_eta_did_not_move_the_ranking():
    """The correction changes FEASIBILITY and nothing else, and that is checkable.

    `_score` min-max normalizes the ETAs across the candidate set, and
    `est_duration_s` is a property of the JOB, not of the target — so removing it
    subtracts the same constant from every candidate, and a constant shift leaves a
    min-max normalization identical.
    """
    from livestack_node.fleet_scheduler import _eta
    from livestack_node.fleet_scheduler import CostModel, Job, Target, Tier

    job = Job(id="j", kind="align", owner="o", est_duration_s=900.0)
    running = Target(id="r", host_id="h", tier=Tier.LOCAL, capacity={"concurrency": 1.0})
    cold = Target(id="c", host_id="h", tier=Tier.SPOT, capacity={"concurrency": 1.0},
                  cost=CostModel(per_hour=1.0), provision_latency_s=240.0,
                  running=False, elastic=True)
    now_etas = [_eta(running, job), _eta(cold, job)]
    then_etas = [e + job.est_duration_s for e in now_etas]      # the old formula
    def norm(xs):
        lo, hi = min(xs), max(xs)
        return [0.0 for _ in xs] if hi - lo < 1e-9 else [(x - lo) / (hi - lo) for x in xs]
    assert norm(now_etas) == norm(then_etas)


# -- distance matters per SLA ------------------------------------------------

def test_a_batch_job_crosses_an_ocean_to_reach_an_idle_card():
    """§5.3's whole purpose: the digest is what makes an idle card earn its keep
    and relieves tower0's single 3090. A flat distance weight defeats that — the
    caller is always nearest to itself, so every digest would stay home. A batch
    job runs for 40 s and pays the round trip ONCE, so distance is scaled down
    to 0.1 for it."""
    view = _view(
        {"zz-tower0": [_node("http://100.64.0.3:8766/livestack", "zz-tower0",
                             probe_ms=0.5, in_flight=3, pressure=0.81,
                             units=ALIGN_COLD)],
         "xc-tower-ubuntu": [_node("http://100.64.0.18:8766/livestack",
                                   "xc-tower-ubuntu", probe_ms=708.0, in_flight=0,
                                   pressure=0.21, units=ALIGN_RESIDENT)]},
        links={"zz-tower0": {"xc-tower-ubuntu": 708.0}},
    )
    # From tower0's OWN vantage: home is 0 ms away and three deep; Toronto is
    # 708 ms away, idle, and already holding the model.
    r = admit(view, kind="align", sla="batch", vantage="host:zz-tower0",
              estimate_s=40.0, now=1000.0)
    assert r["target"]["host_id"] == "xc-tower-ubuntu", r["reason"]

    # The SAME fleet, the same instant, an INTERACTIVE request: 708 ms is now
    # most of what the user would feel, so it stays home.
    quick = admit(view, kind="align", sla="interactive", vantage="host:zz-tower0",
                  estimate_s=5.0, now=1000.0)
    assert quick["target"]["host_id"] == "zz-tower0", quick["reason"]


def test_batch_still_prefers_near_when_nothing_else_separates_them():
    """Scaled down, not dropped."""
    view = _view({"h": [
        _node("http://near/livestack", "h", probe_ms=2.0, in_flight=0, units=ALIGN_RESIDENT),
        _node("http://far/livestack", "h", probe_ms=900.0, in_flight=0, units=ALIGN_RESIDENT),
    ]})
    r = admit(view, kind="align", sla="batch", now=1000.0)
    assert r["target"]["target_id"] == "http://near", r["reason"]


# -- per-account fairness ----------------------------------------------------
#
# Two mechanisms answering different questions. A CEILING answers "may this
# account have another slot at all"; FAIR SHARE answers "whose job goes first
# when several want the same room". A fleet with only the first is fair and
# rigid; with only the second, one account still takes everything as long as it
# asks steadily.

from livestack_node.fleet_scheduler import (
    FleetState, Job, Queue, Sla, over_quota, quota_for, schedule,
)

QUOTA_2 = SchedulerPolicy(weights=Weights(), max_concurrent_per_account=2)


def test_an_account_at_its_ceiling_is_refused_with_the_count():
    """A refusal, not a demotion. A quiet demotion looks identical to a slow
    fleet, and the tenant files a latency bug instead of asking for quota."""
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="acct-a",
              usage={"acct-a": 2}, policy=QUOTA_2, now=1000.0)
    assert r["granted"] is False
    assert r["refused"] == "account_quota"
    assert "acct-a holds 2 of 2 slot(s)" in r["reason"]
    # And it says the room existed — which is what tells "you are capped" apart
    # from "the fleet is full".
    assert "could otherwise have run align" in r["reason"]


def test_the_same_fleet_still_serves_a_different_account():
    """The ceiling is per account, so one tenant hitting it must not look like
    an outage to the next."""
    capped = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="acct-a",
                   usage={"acct-a": 2}, policy=QUOTA_2, now=1000.0)
    other = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="acct-b",
                  usage={"acct-a": 2}, policy=QUOTA_2, now=1000.0)
    assert capped["granted"] is False
    assert other["granted"] is True


def test_no_ceiling_is_the_default_and_changes_nothing():
    """An unset bound must never start refusing work on the deploy that
    introduces it — the same rule the prune windows and the ledger age window
    already follow."""
    assert SchedulerPolicy().max_concurrent_per_account is None
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="acct-a",
              usage={"acct-a": 99}, now=1000.0)
    assert r["granted"] is True
    assert r["refused"] is None


def test_a_per_account_override_beats_the_fleet_wide_ceiling():
    pol = SchedulerPolicy(max_concurrent_per_account=1,
                          account_quotas={"media-corpus": 4})
    assert quota_for("anyone", pol) == 1
    assert quota_for("media-corpus", pol) == 4
    ok = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="media-corpus",
               usage={"media-corpus": 3}, policy=pol, now=1000.0)
    assert ok["granted"] is True
    capped = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="stranger",
                   usage={"stranger": 1}, policy=pol, now=1000.0)
    assert capped["granted"] is False


def test_a_burst_from_one_account_cannot_slip_past_the_ceiling_together():
    """The check is per JOB against usage that grows as the pass admits — which
    is exactly how a naive per-request check is defeated: send four at once and
    every one of them sees the pre-burst count."""
    targets, _ = targets_from_view(BUSY_CN_IDLE_NA, "align")
    jobs = tuple(
        Job(id=f"j{i}", kind="align", owner="acct-a", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1000.0)
        for i in range(5)
    )
    plan = schedule(FleetState(targets=targets, jobs=jobs, now=1000.0), QUOTA_2)
    admitted = [a for a in plan.actions if hasattr(a, "target_id")]
    queued = [a for a in plan.actions if isinstance(a, Queue)]
    assert len(admitted) == 2, f"admitted {len(admitted)}, ceiling was 2"
    assert len(queued) == 3
    assert all("account quota" in q.reason for q in queued)


def test_usage_already_held_counts_against_the_burst():
    targets, _ = targets_from_view(BUSY_CN_IDLE_NA, "align")
    jobs = tuple(
        Job(id=f"j{i}", kind="align", owner="acct-a", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1000.0)
        for i in range(3)
    )
    plan = schedule(
        FleetState(targets=targets, jobs=jobs, now=1000.0, usage={"acct-a": 1}),
        QUOTA_2)
    assert len([a for a in plan.actions if hasattr(a, "target_id")]) == 1


def test_fair_share_serves_the_quieter_account_first_under_contention():
    """Whose job goes first when both want the same room. `acct-busy` already
    holds three slots; `acct-quiet` holds none and asked a moment later."""
    targets, _ = targets_from_view(BUSY_CN_IDLE_NA, "align")
    jobs = (
        Job(id="busy-1", kind="align", owner="acct-busy", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1000.0),
        Job(id="quiet-1", kind="align", owner="acct-quiet", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1001.0),
    )
    state = FleetState(targets=targets, jobs=jobs, now=1000.0,
                       usage={"acct-busy": 3})
    plan = schedule(state, SchedulerPolicy(fair_share_penalty_s=30.0))
    first = next(a for a in plan.actions if hasattr(a, "target_id"))
    assert first.job_id == "quiet-1", "the account already being served yields"

    # With the penalty off, the earlier job wins on plain EDF — so the mechanism
    # under test is the penalty and not the fixture.
    plain = schedule(state, SchedulerPolicy(fair_share_penalty_s=0.0))
    assert next(a for a in plain.actions if hasattr(a, "target_id")).job_id == "busy-1"


def test_fair_share_does_not_starve_an_urgent_job_behind_an_idle_accounts_batch():
    """It composes with EDF rather than replacing it. A held slot costs 30 s of
    urgency; an interactive deadline is worth far more than that, so a tight job
    from a busy account still beats a batch job from an idle one."""
    targets, _ = targets_from_view(BUSY_CN_IDLE_NA, "align")
    jobs = (
        Job(id="urgent", kind="align", owner="acct-busy", need={"concurrency": 1.0},
            est_duration_s=5.0, sla=Sla.INTERACTIVE, created_at=1000.0),
        Job(id="bulk", kind="align", owner="acct-quiet", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1000.0),
    )
    plan = schedule(
        FleetState(targets=targets, jobs=jobs, now=1000.0, usage={"acct-busy": 4}),
        SchedulerPolicy())
    assert next(a for a in plan.actions if hasattr(a, "target_id")).job_id == "urgent"


def test_fair_share_is_a_no_op_on_a_single_tenant_fleet():
    """Every job carries the same owner, so every job takes the same penalty and
    the order is exactly what it was."""
    targets, _ = targets_from_view(BUSY_CN_IDLE_NA, "align")
    jobs = tuple(
        Job(id=f"j{i}", kind="align", owner="me", need={"concurrency": 1.0},
            est_duration_s=40.0, sla=Sla.BATCH, created_at=1000.0 + i)
        for i in range(3)
    )
    state = FleetState(targets=targets, jobs=jobs, now=1000.0, usage={"me": 7})
    with_fair = [a.job_id for a in schedule(state, SchedulerPolicy()).actions
                 if hasattr(a, "target_id")]
    without = [a.job_id for a in
               schedule(state, SchedulerPolicy(fair_share_penalty_s=0.0)).actions
               if hasattr(a, "target_id")]
    assert with_fair == without


def test_over_quota_states_the_rule_it_applied():
    pol = SchedulerPolicy(max_concurrent_per_account=3)
    assert over_quota("a", {"a": 2}, pol) is None
    msg = over_quota("a", {"a": 3}, pol)
    assert msg and "a holds 3 of 3" in msg
    assert over_quota("a", {"a": 3}, SchedulerPolicy()) is None


# -- the target choice as a policy (scheduler-policy-routine task 3.2) --------

def test_admit_decides_under_the_callers_decision_id_and_hands_back_the_decision():
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", owner="media-corpus",
              now=1000.0, decision_id="01JDECISIONIDAAAAAAAAAAAAA")
    assert r["decision_id"] == "01JDECISIONIDAAAAAAAAAAAAA"
    d = r["policy_decision"]
    assert d["decision_id"] == "01JDECISIONIDAAAAAAAAAAAAA"
    assert d["chosen"] == r["target"]["target_id"]
    assert [c["id"] for c in d["candidates"]] == [row["id"] for row in d["rows"]]


def test_a_quota_refusal_never_reaches_the_choice():
    r = admit(BUSY_CN_IDLE_NA, kind="align", owner="acct",
              policy=SchedulerPolicy(max_concurrent_per_account=1),
              usage={"acct": 1}, now=1000.0, decision_id="01JDECISIONIDAAAAAAAAAAAAB")
    assert r["refused"] == "account_quota" and r["policy_decision"] is None
