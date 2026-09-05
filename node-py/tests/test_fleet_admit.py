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
    assert "not ready" in filtered["http://cold"]
    assert "does not host align" in filtered["http://tts"]


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


def test_an_interactive_job_too_slow_for_its_own_sla_is_refused():
    """Found by the tests, not by review: INTERACTIVE carries a 30 s deadline
    slack, so a 60 s job under it is infeasible EVERYWHERE and the fleet says so
    rather than granting a target that cannot meet the promise. It is the
    scheduler's existing deadline logic working — worth pinning here because the
    refusal reads like a bug until you see which term produced it."""
    r = admit(BUSY_CN_IDLE_NA, kind="align", sla="interactive", estimate_s=60.0,
              now=1000.0)
    assert r["granted"] is False
    assert "no fleet target can run align" in r["reason"]
    # A batch job of the same length is fine — the difference is the promise,
    # not the work.
    assert admit(BUSY_CN_IDLE_NA, kind="align", sla="batch", estimate_s=60.0,
                 now=1000.0)["granted"] is True


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
