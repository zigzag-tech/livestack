"""Tests for fleet_dispatch: a scheduler plan is executed against a fake provider
registry. Verifies Provision -> lease+run+teardown, offer fall-through, and that
Queue/exhaustion surface as honest statuses. No real renting."""
from livestack_node.fleet_scheduler import (
    CostModel, FleetState, Job, Sla, Target, Tier, schedule,
)
from livestack_node.fleet_dispatch import dispatch
from livestack_node.provision import (
    CapacityError, ComputeHandle, ComputeSpec, Offer, Provisioner,
)


class FakeProvisioner(Provisioner):
    provider = "fake"

    def __init__(self, *, n_offers=2, fail_all=False):
        super().__init__()
        self._n_offers = n_offers
        self.fail_all = fail_all
        self.ran_on: list[str] = []
        self.terminated: list[str] = []
        self._i = 0

    def offers(self, spec):
        return [Offer(provider="fake", sku=f"GPU{k}", zone="z", price_per_hr=0.1 * (k + 1))
                for k in range(self._n_offers)]

    def acquire(self, spec, offer=None):
        if self.fail_all:
            raise CapacityError("fake: out of stock")
        self._i += 1
        iid = f"fake-{self._i}"
        self._live.add(iid)
        return ComputeHandle(provider="fake", instance_id=iid, host="h", port=22, offer=offer)

    def _terminate(self, instance_id):
        self.terminated.append(instance_id)

    def list_instances(self, name_prefix):
        return []


def _pool(**kw):
    base = dict(id="runpod-pool", host_id="runpod", tier=Tier.LAST_RESORT,
                capacity={"slot": 1.0}, cost=CostModel(per_hour=0.2),
                provision_latency_s=60.0, running=False, elastic=True,
                max_instances=4, labels={"provider": "fake"})
    base.update(kw)
    return Target(**base)


def _job(**kw):
    base = dict(id="train:t", kind="train", need={"slot": 1.0},
                sla=Sla.NORMAL, est_duration_s=60.0, selector={"provider": "fake"})
    base.update(kw)
    return Job(**base)


SPEC = ComputeSpec(name="lora-t")


def test_schedule_emits_provision_for_elastic_pool():
    plan = schedule(FleetState(targets=(_pool(),), jobs=(_job(),)))
    from livestack_node.fleet_scheduler import Provision
    prov = plan.of(Provision)
    assert len(prov) == 1 and prov[0].job_id == "train:t"


def test_dispatch_provision_runs_and_tears_down():
    p = FakeProvisioner()
    plan = schedule(FleetState(targets=(_pool(),), jobs=(_job(),)))
    ran = []
    res = dispatch(plan, {"runpod-pool": _pool()}, {"fake": p},
                   run_job=lambda job_id, h: ran.append((job_id, h.instance_id)),
                   spec_for=lambda job_id: SPEC)
    assert res == {"train:t": "done"}
    assert ran == [("train:t", "fake-1")]          # workload ran on the leased box
    assert p.terminated == ["fake-1"]              # and it was torn down


def test_dispatch_workload_failure_falls_through_offers():
    p = FakeProvisioner(n_offers=3)
    plan = schedule(FleetState(targets=(_pool(),), jobs=(_job(),)))
    calls = {"n": 0}

    def flaky(job_id, h):
        calls["n"] += 1
        if calls["n"] < 3:                          # first two boxes "fail"
            raise RuntimeError("flaky host")

    res = dispatch(plan, {"runpod-pool": _pool()}, {"fake": p},
                   run_job=flaky, spec_for=lambda job_id: SPEC)
    assert res == {"train:t": "done"}
    assert calls["n"] == 3                          # fell through to the 3rd offer
    assert p.terminated == ["fake-1", "fake-2", "fake-3"]  # every box torn down


def test_dispatch_all_offers_exhausted_is_failed():
    p = FakeProvisioner(fail_all=True)
    plan = schedule(FleetState(targets=(_pool(),), jobs=(_job(),)))
    res = dispatch(plan, {"runpod-pool": _pool()}, {"fake": p},
                   run_job=lambda job_id, h: None, spec_for=lambda job_id: SPEC)
    assert res == {"train:t": "failed"}
    assert p.terminated == []                       # nothing created -> nothing leaked


def test_dispatch_queue_when_deadline_infeasible():
    # deadline in the past relative to provision+run -> scheduler Queues it
    job = _job(deadline=1.0)
    plan = schedule(FleetState(targets=(_pool(),), jobs=(job,), now=0.0))
    p = FakeProvisioner()
    res = dispatch(plan, {"runpod-pool": _pool()}, {"fake": p},
                   run_job=lambda job_id, h: None, spec_for=lambda job_id: SPEC)
    assert res == {"train:t": "queued"}
    assert p.terminated == []
