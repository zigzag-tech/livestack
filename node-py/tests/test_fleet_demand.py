"""Unmet demand: the join between "the broker said Queue" and "the fleet bursts".

The distinction every test here defends is signal-versus-queue. A queue can hold
work whose caller gave up ten minutes ago, and provisioning for it spends money
for nothing. A decaying register cannot make that mistake, and these pin the
three properties that make it decay correctly: it forgets, it is bounded, and it
never turns a refusal COUNT into a job count.
"""
import pytest

from livestack_node.fleet_demand import DEFAULT_TTL_S, DemandRegister
from livestack_node.fleet_operations import OperationStore
from livestack_node.fleet_ops_api import build_plan
from livestack_node.fleet_pools import parse_pools
from livestack_node.fleet_scheduler import SchedulerPolicy

NOW = 1_700_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def reg(clock=None, **kw):
    return DemandRegister(clock=clock or Clock(), **kw)


# --- it forgets -------------------------------------------------------------
def test_demand_stops_counting_when_the_caller_stops_asking():
    """The property a queue cannot have. A caller that gave up must stop
    justifying a machine."""
    c = Clock()
    r = reg(c, ttl_s=120.0)
    r.record(kind="asr", owner="a", reason="full")
    assert len(r.live()) == 1
    c.advance(119)
    assert len(r.live()) == 1, "still plausibly waiting"
    c.advance(2)
    assert r.live() == [], "nobody has asked in over a TTL; this is not demand"


def test_a_caller_that_keeps_retrying_keeps_the_signal_alive():
    c = Clock()
    r = reg(c, ttl_s=120.0)
    for _ in range(5):
        r.record(kind="asr", owner="a", reason="full")
        c.advance(60)
    live = r.live()
    assert len(live) == 1 and live[0].count == 5


def test_a_restart_forgets_and_that_is_correct():
    """In-memory on purpose: demand older than the restart is not current, and
    reconstructing it would resurrect exactly the abandoned requests the TTL
    exists to forget."""
    r = reg()
    r.record(kind="asr", owner="a", reason="full")
    assert reg().live() == []


# --- it does not inflate ----------------------------------------------------
def test_forty_refusals_of_one_shape_are_one_job_not_forty():
    """Turning a refusal count into a job count is how a brief spike rents a
    datacentre. A pool instance serves several concurrent jobs; the pool's own
    ceiling and the next tick are what scale it further."""
    r = reg()
    for _ in range(40):
        r.record(kind="asr", owner="a", reason="full")
    jobs = r.jobs()
    assert len(jobs) == 1
    assert r.live()[0].count == 40


def test_distinct_shapes_stay_distinct():
    r = reg()
    r.record(kind="asr", owner="a", sla="normal")
    r.record(kind="asr", owner="b", sla="normal")
    r.record(kind="asr", owner="a", sla="batch")
    r.record(kind="align", owner="a", sla="normal")
    assert len({j["job_id"] for j in r.jobs()}) == 4


def test_the_longest_estimate_anyone_asked_for_wins():
    """Under-stating it is how a burst lands a job on a machine that cannot
    finish it inside its deadline."""
    r = reg()
    r.record(kind="asr", owner="a", est_duration_s=20.0)
    r.record(kind="asr", owner="a", est_duration_s=300.0)
    r.record(kind="asr", owner="a", est_duration_s=45.0)
    assert r.jobs()[0]["est_duration_s"] == 300.0


def test_a_synthesised_job_is_marked_as_one():
    r = reg()
    r.record(kind="asr", owner="acct_a", sla="batch")
    assert r.jobs()[0]["job_id"].startswith("demand:")


# --- it is bounded ----------------------------------------------------------
def test_the_register_is_bounded_and_drops_the_stalest_first():
    c = Clock()
    r = reg(c, max_entries=3, ttl_s=10_000.0)
    for i in range(6):
        r.record(kind=f"kind-{i}", owner="a")
        c.advance(1)
    live = r.live()
    assert len(live) == 3
    assert {d.kind for d in live} == {"kind-3", "kind-4", "kind-5"}


def test_the_default_ttl_is_short_enough_to_mean_now():
    assert DEFAULT_TTL_S <= 300, "a long TTL turns a signal back into a queue"


# --- it reaches a plan ------------------------------------------------------
POOLS = parse_pools(
    '[{"id":"heyuan-spot","provider":"aliyun","tier":"SPOT","region":"cn-heyuan",'
    ' "instance_type":"ecs.g8i.2xlarge","cost_per_hour":2.1,"max_instances":2,'
    ' "kinds":["asr"]}]')


def view(in_flight):
    return {"generated_at": NOW, "hosts": {"h1": {"nodes": [
        {"peer": "http://a/livestack", "state": "fresh", "ready": True,
         "kinds": ["asr"], "detail": "resident", "unseen_seconds": 0.0,
         "device_id": "d", "load": {"in_flight": in_flight}}]}}}


def plan_for(tmp_path, jobs, v=None):
    return build_plan(v or view(4), jobs, owner="acct_a",
                      policy=SchedulerPolicy(), pools=POOLS,
                      store=OperationStore(str(tmp_path / "ops.sqlite3"),
                                           clock=lambda: NOW),
                      usage={}, now=NOW)


def test_demand_the_broker_could_not_place_produces_a_provision(tmp_path):
    """The seam, end to end: a refusal becomes a burst decision."""
    r = reg()
    r.record(kind="asr", owner="acct_a", sla="batch", est_duration_s=40.0,
             reason="no feasible target meets the deadline now")
    plan = plan_for(tmp_path, r.jobs())
    assert [a["type"] for a in plan["actions"]] == ["provision"]
    assert plan["actions"][0]["target_id"] == "heyuan-spot"


def test_interactive_demand_still_never_bursts(tmp_path):
    """240 s of provisioning does not fit 30 s of slack. The seam does not widen
    what an SLA permits — it only makes the demand reachable."""
    r = reg()
    r.record(kind="asr", owner="acct_a", sla="interactive", est_duration_s=8.0)
    plan = plan_for(tmp_path, r.jobs())
    assert [a["type"] for a in plan["actions"]] == ["queue"]


def test_demand_that_a_free_fleet_can_absorb_is_admitted_not_provisioned(tmp_path):
    r = reg()
    r.record(kind="asr", owner="acct_a", sla="batch")
    plan = plan_for(tmp_path, r.jobs(), v=view(0))
    assert [a["type"] for a in plan["actions"]] == ["admit"]


def test_expired_demand_reaches_no_plan_at_all(tmp_path):
    c = Clock()
    r = reg(c, ttl_s=120.0)
    r.record(kind="asr", owner="acct_a", sla="batch")
    c.advance(200)
    plan = plan_for(tmp_path, r.jobs())
    assert plan["actions"] == [], "a burst for a caller that gave up is money for nothing"
