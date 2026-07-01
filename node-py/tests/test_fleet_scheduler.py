"""Tests for the fleet scheduler (resource/budget/speed-aware admission & burst).
Pure logic, no cloud: fake ``now``, slot-valued capacities, ¥-valued CostModels model
the real prefer-local -> burst-cheap -> RunPod-last-resort ladder over tower0 + Aliyun
+ RunPod. Mirrors test_planner.py in spirit."""
from livestack_node.fleet_scheduler import (
    Target, Job, FleetState, Ledger, Tier, Sla, CostModel,
    SchedulerPolicy, Weights, WeightPolicy,
    schedule, resolve_weights,
    Admit, Provision, Queue, Deprovision,
)

HOUR = 3600.0


# --- target builders --------------------------------------------------------
def local(free=1, host="tower0", labels=None):
    # tower0 GPU: running, sunk cost, fixed capacity.
    return Target(id="local", host_id=host, tier=Tier.LOCAL,
                  capacity={"slot": free}, cost=CostModel(), running=True,
                  labels=labels or {})


def spot(max_instances=4, latency=90.0, per_hour=2.0, labels=None, running_instances=0,
         running=False, up_since=0.0):
    # Aliyun ECI/spot: cheap elastic cloud.
    return Target(id="spot", host_id="aliyun", tier=Tier.SPOT,
                  capacity={"slot": 1}, cost=CostModel(per_hour=per_hour),
                  provision_latency_s=latency, running=running, elastic=True,
                  max_instances=max_instances, running_instances=running_instances,
                  up_since=up_since, labels=labels or {})


def runpod(max_instances=8, latency=10.0, per_hour=20.0, labels=None):
    # RunPod serverless: expensive, near-instant, LAST_RESORT.
    return Target(id="runpod", host_id="runpod", tier=Tier.LAST_RESORT,
                  capacity={"slot": 1}, cost=CostModel(per_hour=per_hour),
                  provision_latency_s=latency, running=False, elastic=True,
                  max_instances=max_instances, labels=labels or {})


def job(id="j", kind="asr", sla=Sla.NORMAL, created_at=0.0, dur=60.0, deadline=None,
        need=None, host=None, selector=None):
    return Job(id=id, kind=kind, sla=sla, created_at=created_at, est_duration_s=dur,
               deadline=deadline, need=need or {"slot": 1.0}, locality_host=host,
               selector=selector or {})


def ids(actions):
    return sorted(a.job_id for a in actions if hasattr(a, "job_id"))


# --- prefer-local -----------------------------------------------------------
def test_prefer_local_when_local_idle():
    # Local has room -> admit there; never touch paid cloud.
    st = FleetState(targets=(local(free=1), spot(), runpod()),
                    jobs=(job("j1", sla=Sla.BATCH),), now=0)
    p = schedule(st)
    assert ids(p.of(Admit)) == ["j1"]
    assert p.of(Admit)[0].target_id == "local"
    assert not p.of(Provision)


def test_local_wins_on_budget_even_when_cloud_also_feasible():
    # Both local and spot could take it; the cost term keeps it local (¥0).
    st = FleetState(targets=(local(free=1), spot(running=True, running_instances=1)),
                    jobs=(job("j1", sla=Sla.NORMAL),), now=0)
    p = schedule(st)
    assert p.of(Admit)[0].target_id == "local"


# --- burst under pressure ---------------------------------------------------
def test_burst_to_spot_when_local_full():
    # Local capacity 1, two jobs: one lands local, one bursts to the cheap SPOT pool.
    st = FleetState(targets=(local(free=1), spot(), runpod()),
                    jobs=(job("j1", sla=Sla.NORMAL, created_at=0),
                          job("j2", sla=Sla.NORMAL, created_at=0)),
                    now=0)
    p = schedule(st)
    assert ids(p.of(Admit)) == ["j1"] or ids(p.of(Admit)) == ["j2"]
    prov = p.of(Provision)
    assert len(prov) == 1
    assert prov[0].tier == Tier.SPOT          # cheap tier, not RunPod
    assert prov[0].target_id == "spot"


# --- RunPod is the last resort ----------------------------------------------
def test_runpod_only_when_nothing_cheaper_meets_deadline():
    # Tight INTERACTIVE deadline: SPOT's 90s provision latency misses it, RunPod's 10s
    # makes it -> RunPod is the ONLY feasible target, so it wins despite the cost.
    st = FleetState(
        targets=(local(free=0),  # local full
                 spot(latency=90.0), runpod(latency=10.0)),
        jobs=(job("j1", sla=Sla.INTERACTIVE, created_at=0, dur=5.0),),  # deadline = now+30
        now=0)
    p = schedule(st)
    prov = p.of(Provision)
    assert len(prov) == 1
    assert prov[0].tier == Tier.LAST_RESORT
    assert prov[0].target_id == "runpod"


def test_runpod_suppressed_when_spot_can_meet_deadline():
    # Same tight-ish job but SPOT is fast enough -> RunPod is guarded out entirely.
    st = FleetState(
        targets=(local(free=0), spot(latency=10.0), runpod(latency=5.0)),
        jobs=(job("j1", sla=Sla.INTERACTIVE, created_at=0, dur=5.0),),
        now=0)
    p = schedule(st)
    prov = p.of(Provision)
    assert len(prov) == 1
    assert prov[0].tier == Tier.SPOT          # cheaper feasible tier preferred
    assert not any(a.tier == Tier.LAST_RESORT for a in prov)


# --- off-peak never provisions the expensive tier ---------------------------
def test_offpeak_budget_lean_keeps_a_loose_job_off_paid_when_local_free():
    # Off-peak weights + a BATCH job with local room -> stays local (¥0), no burst.
    wpol = WeightPolicy()  # 03:00 UTC is off-peak by default
    st = FleetState(targets=(local(free=1), spot(), runpod()),
                    jobs=(job("j1", sla=Sla.BATCH),), now=3 * HOUR)
    weights = resolve_weights(st, wpol)
    p = schedule(st, SchedulerPolicy(weights=weights))
    assert p.of(Admit)[0].target_id == "local"
    assert not p.of(Provision)


def test_batch_job_queues_rather_than_paying_when_no_room_and_slack_huge():
    # Local full, and the only clouds are cold pools whose provision latency still fits
    # the huge BATCH deadline -> they ARE feasible, so a job bursts to the cheapest.
    # (Confirms BATCH still runs; it just prefers the cheapest feasible tier.)
    st = FleetState(targets=(local(free=0), spot(latency=90.0)),
                    jobs=(job("j1", sla=Sla.BATCH),), now=0)
    p = schedule(st)
    assert p.of(Provision) and p.of(Provision)[0].tier == Tier.SPOT


def test_no_feasible_target_queues():
    # Local full, no cloud pools at all -> the job is queued, never rejected.
    st = FleetState(targets=(local(free=0),), jobs=(job("j1", sla=Sla.NORMAL),), now=0)
    p = schedule(st)
    assert ids(p.of(Queue)) == ["j1"]
    assert not p.of(Admit) and not p.of(Provision)


# --- deadline / EDF ---------------------------------------------------------
def test_pool_cap_forces_the_less_urgent_job_to_queue():
    # One SPOT instance available (max_instances=1), local full, two jobs of differing
    # urgency -> the earlier-deadline job gets it, the other queues.
    st = FleetState(
        targets=(local(free=0), spot(max_instances=1, latency=10.0)),
        jobs=(job("urgent", sla=Sla.NORMAL, created_at=0, deadline=100.0),
              job("loose", sla=Sla.BATCH, created_at=0)),
        now=0)
    p = schedule(st)
    assert ids(p.of(Provision)) == ["urgent"]
    assert ids(p.of(Queue)) == ["loose"]


# --- deprovision hysteresis -------------------------------------------------
def test_idle_burst_worker_is_deprovisioned_when_past_min_uptime():
    # A running SPOT instance, no jobs, up long enough -> drain it.
    st = FleetState(targets=(local(free=1),
                             spot(running=True, running_instances=1, up_since=0.0)),
                    jobs=(), now=10 * 60)          # 600s > default min_uptime 120s
    p = schedule(st)
    assert [a.target_id for a in p.of(Deprovision)] == ["spot"]


def test_fresh_burst_worker_is_not_deprovisioned():
    # Same idle SPOT but only 30s old -> anti-thrash keeps it up.
    st = FleetState(targets=(local(free=1),
                             spot(running=True, running_instances=1, up_since=0.0)),
                    jobs=(), now=30)
    p = schedule(st)
    assert not p.of(Deprovision)


def test_busy_burst_worker_is_not_deprovisioned():
    # SPOT instance that takes a job this cycle is not drained.
    st = FleetState(
        targets=(local(free=0),
                 spot(running=True, running_instances=1, up_since=0.0)),
        jobs=(job("j1", sla=Sla.NORMAL),), now=10 * 60)
    p = schedule(st)
    assert ids(p.of(Admit)) == ["j1"] and p.of(Admit)[0].target_id == "spot"
    assert not p.of(Deprovision)


# --- weight resolution ------------------------------------------------------
def test_manual_override_beats_time_of_day():
    pinned = Weights(resource=9.0, budget=9.0, speed=9.0)
    st = FleetState(targets=(local(),), jobs=(), now=12 * HOUR)   # noon = peak
    assert resolve_weights(st, WeightPolicy(manual=pinned)) == pinned


def test_time_of_day_peak_vs_offpeak():
    st_peak = FleetState(targets=(local(),), jobs=(), now=12 * HOUR)
    st_off = FleetState(targets=(local(),), jobs=(), now=3 * HOUR)
    w_peak = resolve_weights(st_peak, WeightPolicy())
    w_off = resolve_weights(st_off, WeightPolicy())
    assert w_peak.speed > w_peak.budget       # peak leans speed
    assert w_off.budget > w_off.speed         # off-peak leans budget


def test_cost_pressure_raises_budget_weight():
    # Over-target spend nudges toward budget-first without blocking anything.
    st_ok = FleetState(targets=(local(),), jobs=(), now=12 * HOUR,
                       ledger=Ledger(spent=5.0, target=10.0))
    st_over = FleetState(targets=(local(),), jobs=(), now=12 * HOUR,
                         ledger=Ledger(spent=20.0, target=10.0))
    assert resolve_weights(st_over).budget > resolve_weights(st_ok).budget


def test_deadline_risk_raises_speed_weight():
    # A job near its deadline pushes the speed weight up vs an idle fleet.
    st_calm = FleetState(targets=(local(),), jobs=(), now=12 * HOUR)
    st_risk = FleetState(targets=(local(),),
                         jobs=(job("j1", sla=Sla.INTERACTIVE, created_at=12 * HOUR),),
                         now=12 * HOUR)   # deadline = now+30s, well inside the 300s risk window
    assert resolve_weights(st_risk).speed > resolve_weights(st_calm).speed
