"""Consumer client: embedded leasing keeps a unit warm for the call; no-op mode
degrades gracefully when no manager is reachable."""

from livestack_node.client import lease
from livestack_node.lease import Capability
from livestack_node.residence import ResidenceController
from livestack_node.runtime import FakeRuntime


def make_controller():
    runtime = FakeRuntime()
    units = {"diarize": Capability(kind="diarize", host_id="h", lease_ttl_seconds=100)}
    return ResidenceController("h", runtime, units), runtime


def test_embedded_lease_warms_then_releases():
    ctrl, runtime = make_controller()
    with lease("diarize", controller=ctrl, heartbeat_interval=1000):
        assert runtime.resident() == ["diarize"]  # warm during the call
    assert runtime.resident() == []  # released after -> evicted


def test_noop_lease_when_no_manager():
    # No controller, no base_url -> yields a no-op lease, never raises.
    with lease("diarize") as handle:
        assert handle.lease_id is None


def test_remote_lease_degrades_when_unreachable():
    # Unreachable endpoint -> no-op, call still proceeds (standalone path).
    with lease("diarize", base_url="http://127.0.0.1:1/livestack") as handle:
        assert handle.lease_id is None
