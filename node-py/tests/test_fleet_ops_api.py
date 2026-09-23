"""The control plane's three routes, tested at the decision layer.

Deliberately not through a socket: what is interesting here is the ANSWER —
what the plan reports, what the claim refuses, what a stale plan version does —
and a decision that needs a server to test is a decision nobody tests. The
routes in `hostd` are thin wrappers over exactly these functions.
"""
import pytest

from fake_worker_provider import FakeWorkerProvider
from livestack_node.fleet_operations import ANNOUNCED, CREATED, OperationStore
from livestack_node.fleet_ops_api import (
    ActionRefused, build_plan, deprovision, kind_labels, plan_is_current,
    plan_targets, policy_digest, provision,
)
from livestack_node.fleet_pools import parse_pools
from livestack_node.fleet_scheduler import SchedulerPolicy

NOW = 1_700_000_000.0

POOLS = parse_pools(
    '[{"id":"heyuan-spot","provider":"fake","tier":"SPOT","region":"cn-heyuan",'
    ' "instance_type":"ecs.g8i.2xlarge","cost_per_hour":2.1,"max_instances":2,'
    ' "kinds":["llm"]}]')


def node(peer, host, *, ready=True, in_flight=0, kinds=("llm",), state="fresh",
         operation_id=None):
    n = {"peer": peer, "state": state, "ready": ready, "kinds": list(kinds),
         "detail": "resident", "unseen_seconds": 0.0, "device_id": f"{host}/dev"}
    if in_flight is not None:
        n["load"] = {"in_flight": in_flight}
    if operation_id:
        n["operation_id"] = operation_id
    return n


def view(*nodes, generated_at=NOW):
    return {"generated_at": generated_at,
            "hosts": {"h1": {"nodes": list(nodes)}}}


def store(tmp_path):
    return OperationStore(str(tmp_path / "ops.sqlite3"), clock=lambda: NOW)


def jobs(n=1, kind="llm"):
    return [{"job_id": f"job-{i}", "kind": kind, "sla": "normal"} for i in range(n)]


def plan(tmp_path, v, job_rows, *, pools=POOLS, policy=None, usage=None,
         st=None, **kw):
    return build_plan(v, job_rows, owner="acct_a",
                      policy=policy or SchedulerPolicy(), pools=pools,
                      store=st or store(tmp_path), usage=usage or {},
                      now=NOW, **kw)


# --- the plan reads ---------------------------------------------------------
def test_a_free_local_node_is_admitted_and_nothing_is_provisioned(tmp_path):
    out = plan(tmp_path, view(node("http://a/livestack", "h1")), jobs(1))
    assert [a["type"] for a in out["actions"]] == ["admit"]
    assert out["actions"][0]["target_id"] == "http://a"


def test_a_full_fleet_bursts_into_the_declared_pool(tmp_path):
    out = plan(tmp_path, view(node("http://a/livestack", "h1", in_flight=4)),
               jobs(1))
    assert [a["type"] for a in out["actions"]] == ["provision"]
    assert out["actions"][0]["target_id"] == "heyuan-spot"
    assert out["actions"][0]["tier"] == "SPOT"


def test_with_no_pools_declared_a_full_fleet_queues_rather_than_bursting(tmp_path):
    out = plan(tmp_path, view(node("http://a/livestack", "h1", in_flight=4)),
               jobs(1), pools=())
    assert [a["type"] for a in out["actions"]] == ["queue"]


def test_the_plan_reserves_nothing_so_two_calls_agree(tmp_path):
    st = store(tmp_path)
    v = view(node("http://a/livestack", "h1", in_flight=4))
    first = plan(tmp_path, v, jobs(1), st=st)
    second = plan(tmp_path, v, jobs(1), st=st)
    assert first["actions"] == second["actions"]
    assert first["reservations"] == [] == second["reservations"]


def test_an_unreported_in_flight_surfaces_as_uncertainty_not_as_free(tmp_path):
    """`targets_from_view` credits a silent node with its full concurrency, which
    is the right default and the exact thing a plan must not forget it did."""
    n = node("http://a/livestack", "h1")
    n.pop("load")
    out = plan(tmp_path, view(n), jobs(1))
    assert [u["target_id"] for u in out["uncertainty"]] == ["http://a"]
    assert out["uncertainty"][0]["field"] == "in_flight"
    assert "assumed, not measured" in out["uncertainty"][0]["reason"]


def test_every_excluded_target_and_pool_appears_with_its_reason(tmp_path):
    out = plan(tmp_path,
               view(node("http://dead/livestack", "h1", state="mia"),
                    node("http://busy/livestack", "h1", in_flight=4)),
               jobs(1, kind="asr"))
    reasons = {e.get("target_id") or e.get("pool_id"): e["reason"]
               for e in out["excluded"]}
    assert "does not host asr" in reasons["http://dead"]
    assert "does not serve asr" in reasons["heyuan-spot"]


def test_a_region_policy_that_removes_every_pool_says_so(tmp_path):
    out = plan(tmp_path, view(node("http://a/livestack", "h1", in_flight=4)),
               jobs(1), allow_regions=("na",))
    assert [a["type"] for a in out["actions"]] == ["queue"]
    assert any("caller allows na" in e["reason"] for e in out["excluded"])


def test_one_node_serving_two_kinds_shares_one_capacity_budget(tmp_path):
    """The reason kinds live in labels. A per-kind target list would hand the
    same free slot to an llm job and an asr job at once."""
    v = view(node("http://a/livestack", "h1", kinds=("llm", "asr"), in_flight=3))
    out = plan(tmp_path, v, [{"job_id": "j-llm", "kind": "llm"},
                             {"job_id": "j-asr", "kind": "asr"}])
    admitted = [a for a in out["actions"] if a["type"] == "admit"]
    assert len(admitted) == 1, out["actions"]


def test_pending_creates_are_counted_against_the_owner_ceiling(tmp_path):
    st = store(tmp_path)
    st.claim(job_id="earlier", owner="acct_a", target_id="heyuan-spot",
             idempotency_key="k0", provider="fake")
    out = plan(tmp_path, view(node("http://a/livestack", "h1")), jobs(1),
               policy=SchedulerPolicy(account_quotas={"acct_a": 1}), st=st)
    assert [a["type"] for a in out["actions"]] == ["queue"]
    assert "account quota" in out["actions"][0]["reason"]
    assert [r["state"] for r in out["reservations"]] == ["creating"]


def test_the_plan_version_moves_with_the_policy_and_the_pools(tmp_path):
    v = view(node("http://a/livestack", "h1"))
    base = plan(tmp_path, v, jobs(1))["plan_version"]
    other = plan(tmp_path, v, jobs(1),
                 policy=SchedulerPolicy(account_quotas={"x": 3}))["plan_version"]
    assert base != other
    assert plan(tmp_path, v, jobs(1), pools=())["plan_version"] != base


def test_kind_labels_and_selectors_agree():
    assert kind_labels(["llm", "asr"]) == {"kind.llm": "1", "kind.asr": "1"}


# --- staleness --------------------------------------------------------------
def test_a_plan_from_the_current_policy_is_current():
    pol = SchedulerPolicy()
    version = f"v1.{policy_digest(pol, POOLS)}.{int(NOW)}"
    assert plan_is_current(version, policy=pol, pools=POOLS, now=NOW) is None


def test_a_plan_computed_under_a_different_policy_is_refused():
    version = f"v1.{policy_digest(SchedulerPolicy(), POOLS)}.{int(NOW)}"
    stale = plan_is_current(version, policy=SchedulerPolicy(account_quotas={"a": 1}),
                            pools=POOLS, now=NOW)
    assert "policy or the pool set changed" in stale


def test_an_old_plan_is_refused_with_its_age():
    pol = SchedulerPolicy()
    version = f"v1.{policy_digest(pol, POOLS)}.{int(NOW)}"
    assert "300s old" in plan_is_current(version, policy=pol, pools=POOLS,
                                         now=NOW + 300, max_age_s=120)


def test_a_malformed_plan_version_is_refused_rather_than_ignored():
    assert plan_is_current("", policy=SchedulerPolicy(), pools=POOLS, now=NOW)


# --- the operation spends ---------------------------------------------------
def act(target_id="heyuan-spot", job_id="job-0", kind="llm", type="provision"):
    return {"type": type, "target_id": target_id, "job_id": job_id, "kind": kind}


def do_provision(st, providers, *, key="k1", policy=None, usage=None, **kw):
    ran = []
    out = provision(act(), store=st, pools=POOLS, providers=providers,
                    owner="acct_a", principal="hub", plan_version="v1.x.0",
                    idempotency_key=key, policy=policy or SchedulerPolicy(),
                    usage=usage or {}, announce_env={"LIVESTACK_BROKER_URL": "http://b"},
                    spawn=lambda fn: (ran.append(1), fn()), now=NOW, **kw)
    return out, ran


def test_a_provision_claims_before_it_creates(tmp_path):
    st, p = store(tmp_path), FakeWorkerProvider()
    out, ran = do_provision(st, {"fake": p})
    assert ran == [1]
    op = st.get(out["operation_id"])
    assert op.state == CREATED and op.provider_instance_id == "fake-i-1"
    assert op.plan_version == "v1.x.0" and op.principal == "hub"


def test_an_unknown_pool_is_refused_and_nothing_is_claimed(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ActionRefused) as e:
        provision({"type": "provision", "target_id": "nope"}, store=st,
                  pools=POOLS, providers={}, owner="a", principal=None,
                  plan_version="v", idempotency_key="k", policy=SchedulerPolicy(),
                  usage={}, announce_env={})
    assert e.value.status == 409
    assert st.active() == []


def test_a_provider_with_no_adapter_refuses_before_claiming(tmp_path):
    """501, and — the part that matters — no claim. A claim for a create that
    can never happen holds its owner's quota until somebody notices."""
    st = store(tmp_path)
    with pytest.raises(ActionRefused) as e:
        do_provision(st, {})
    assert e.value.status == 501
    assert st.active() == []


def test_a_quota_refusal_is_a_409_naming_the_ceiling(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ActionRefused) as e:
        do_provision(st, {"fake": FakeWorkerProvider()},
                     policy=SchedulerPolicy(account_quotas={"acct_a": 0}))
    assert e.value.status == 409 and "account quota" in e.value.detail


def test_a_replayed_idempotency_key_does_not_create_again(tmp_path):
    st, p = store(tmp_path), FakeWorkerProvider()
    first, _ = do_provision(st, {"fake": p}, key="same")
    second, ran = do_provision(st, {"fake": p}, key="same")
    assert second["operation_id"] == first["operation_id"]
    assert ran == []                       # no second dispatch
    assert len(p.created) == 1


def test_a_dispatch_thread_that_dies_leaves_the_operation_reconcilable(tmp_path):
    st = store(tmp_path)
    said = []

    class Exploding(FakeWorkerProvider):
        def create(self, **kw):
            raise KeyboardInterrupt("something violent")

    out, _ = do_provision(st, {"fake": Exploding()}, log=said.append)
    # Classified as unknown-effect rather than lost: it is reconcilable, and it
    # still holds its quota until it is reconciled.
    assert st.get(out["operation_id"]).state == "uncertain"


# --- draining ---------------------------------------------------------------
def announced_node(tmp_path, providers):
    st = store(tmp_path)
    out, _ = do_provision(st, providers)
    st.announce(out["operation_id"], ready=True, node="http://burst-1")
    return st, st.get(out["operation_id"])


def test_a_node_this_broker_did_not_provision_is_never_released(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ActionRefused) as e:
        deprovision(act(target_id="http://someone-elses", type="deprovision"),
                    store=st, providers={}, busy=lambda _n: None)
    assert "did not provision it" in e.value.detail


def test_a_busy_node_is_refused_and_the_instance_is_left_alone(tmp_path):
    p = FakeWorkerProvider()
    st, op = announced_node(tmp_path, {"fake": p})
    with pytest.raises(ActionRefused) as e:
        deprovision(act(target_id="http://burst-1", type="deprovision"),
                    store=st, providers={"fake": p},
                    busy=lambda _n: "2 active lease(s)")
    assert "not drainable" in e.value.detail
    assert st.get(op.operation_id).state == ANNOUNCED
    assert p.terminated == []


def test_an_empty_node_is_released_and_the_instance_torn_down(tmp_path):
    p = FakeWorkerProvider()
    st, op = announced_node(tmp_path, {"fake": p})
    out = deprovision(act(target_id="http://burst-1", type="deprovision"),
                      store=st, providers={"fake": p}, busy=lambda _n: None)
    assert out["state"] == "released" and out["teardown"] == "ok"
    assert p.terminated == ["fake-i-1"]


def test_a_teardown_failure_is_reported_rather_than_swallowed(tmp_path):
    """Released-but-still-running is money burning with nobody watching. It must
    not come back looking like a clean teardown."""
    p = FakeWorkerProvider()
    st, op = announced_node(tmp_path, {"fake": p})

    def boom(_iid):
        raise RuntimeError("provider API 500")

    p.terminate = boom
    said = []
    out = deprovision(act(target_id="http://burst-1", type="deprovision"),
                      store=st, providers={"fake": p}, busy=lambda _n: None,
                      log=said.append)
    assert out["teardown"] == "failed" and "500" in out["teardown_error"]
    assert any("may still be billing" in m for m in said)
