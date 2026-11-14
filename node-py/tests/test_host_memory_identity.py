"""Identity combinations cannot be exercised on one host's fixed hardware IDs."""
from types import SimpleNamespace
from livestack_node.hostbroker import HostBroker

GIB = 1 << 30


def broker():
    b = HostBroker([], [], clock=lambda: 1000.0)
    b.peer_report = {"llm": {"at": 2, "host_mem": {"available_bytes": 60 * GIB}}}
    b._peer_device["llm"] = "tower/gpu1"
    b.roster._records["llm"] = SimpleNamespace(host_id="tower-llm-gpu1")
    return b


def test_measured_ram_follows_resolved_physical_device_host():
    b = broker()
    hosts, reserves = b._host_pools({}, [], {"tower/gpu1": "tower"})
    assert hosts == {"tower": {"ram_bytes": 59 * GIB}}
    assert reserves == {"tower": {"ram_bytes": GIB}}


def test_alias_readings_share_one_pool_and_keep_freshest_measurement():
    b = broker()
    b._peer_device["speech"] = "tower/gpu0"
    b.roster._records["speech"] = SimpleNamespace(host_id="tower-speech")
    b.peer_report["speech"] = {"at": 3, "host_mem": {"available_bytes": 30 * GIB}}
    hosts, _ = b._host_pools({}, [], {"tower/gpu1": "tower", "tower/gpu0": "tower"})
    assert hosts == {"tower": {"ram_bytes": 29 * GIB}}


def test_absent_measurement_is_not_invented_by_identity_resolution():
    b = broker()
    b.peer_report["llm"] = {"at": 3}
    hosts, _ = b._host_pools({}, [], {"tower/gpu1": "tower"})
    assert hosts == {}
