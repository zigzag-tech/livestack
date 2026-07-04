"""Tests for the general provisioning harness. A FakeProvisioner records
acquire/terminate calls so we can assert the money-safety invariant — an instance
is ALWAYS torn down — without renting anything."""
from livestack_node.provision import (
    CapacityError,
    ComputeHandle,
    ComputeSpec,
    Instance,
    Offer,
    ProvisionError,
    Provisioner,
    leased,
    reap_orphans,
)


class FakeProvisioner(Provisioner):
    provider = "fake"

    def __init__(self, *, fail_acquire=False, fail_ssh=False, existing=None):
        super().__init__()
        self.fail_acquire = fail_acquire      # simulate out-of-stock (CapacityError)
        self.fail_ssh = fail_ssh              # simulate created-but-unreachable
        self.terminated: list[str] = []
        self.created: list[str] = []
        self._existing = existing or []       # for list_instances / reap
        self._n = 0

    def offers(self, spec):
        return [Offer(provider="fake", sku="FAKE-GPU", zone="z", price_per_hr=0.1)]

    def acquire(self, spec, offer=None):
        if self.fail_acquire:
            raise CapacityError("fake: out of stock")
        self._n += 1
        iid = f"fake-{self._n}"
        self._live.add(iid)                   # register before "reachable"
        self.created.append(iid)
        if self.fail_ssh:
            self.release_id(iid)              # created but unusable -> tear down, then raise
            raise ProvisionError("fake: never reachable")
        return ComputeHandle(provider="fake", instance_id=iid, host="h", port=22)

    def _terminate(self, instance_id):
        self.terminated.append(instance_id)

    def list_instances(self, name_prefix):
        return list(self._existing)


SPEC = ComputeSpec(name="fake-job")


def test_leased_releases_on_normal_exit():
    p = FakeProvisioner()
    with leased(p, SPEC) as h:
        assert h.instance_id == "fake-1"
        assert p._live == {"fake-1"}
    assert p.terminated == ["fake-1"]
    assert p._live == set()                   # tracking cleared


def test_leased_releases_on_workload_exception():
    p = FakeProvisioner()
    try:
        with leased(p, SPEC):
            raise ValueError("workload blew up")
    except ValueError:
        pass
    assert p.terminated == ["fake-1"]         # still torn down
    assert p._live == set()


def test_capacity_error_creates_nothing_to_release():
    p = FakeProvisioner(fail_acquire=True)
    try:
        with leased(p, SPEC):
            raise AssertionError("should not reach the body")
    except CapacityError:
        pass
    assert p.created == [] and p.terminated == []   # nothing created, nothing leaked


def test_acquire_ssh_failure_tears_down_before_raising():
    p = FakeProvisioner(fail_ssh=True)
    try:
        with leased(p, SPEC):
            raise AssertionError("should not reach the body")
    except ProvisionError:
        pass
    assert p.created == ["fake-1"]
    assert p.terminated == ["fake-1"]         # the created-but-unusable box was reaped
    assert p._live == set()


def test_release_id_is_idempotent():
    p = FakeProvisioner()
    with leased(p, SPEC):
        pass
    p.release_id("fake-1")                     # double release: no crash, no re-add
    assert p.terminated.count("fake-1") in (1, 2)   # terminate may be called again, harmlessly
    assert p._live == set()


def test_reap_orphans_kills_only_old_prefixed():
    p = FakeProvisioner(existing=[
        Instance(id="a", name="fake-job-old", age_min=200),
        Instance(id="b", name="fake-job-fresh", age_min=5),
        Instance(id="c", name="other-old", age_min=999),   # wrong prefix
    ])
    reaped = reap_orphans(p, "fake-job", older_than_min=120)
    assert reaped == ["a"]
    assert p.terminated == ["a"]
