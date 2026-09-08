"""host_mem — the meter that answers 'what else is on this machine'.

Harmony arbitrates VRAM, so every meter until now reported a device. A node
occupies host RAM too, and a node with no GPU at all (a build host) occupies
nothing else — a fleet view drawn from device meters shows those machines empty.
"""
import sys

from livestack_node.meters import host_mem


def test_it_reports_this_machines_memory():
    m = host_mem()
    assert m, "no host memory path for this platform"
    assert m["total_bytes"] > 512 * 2**20          # any machine running this
    assert 0 < m["process_rss_bytes"] < m["total_bytes"]
    if "available_bytes" in m:                     # macOS vm_stat may be absent
        assert 0 < m["available_bytes"] <= m["total_bytes"]


def test_it_is_memoised_so_a_polled_page_does_not_fork_per_read():
    """The macOS path shells out to `vm_stat` and `ps`. `/residence` is read by
    the broker on every reconcile and by anything watching it, so an unmemoised
    meter forks twice per node per poll."""
    calls = []
    import livestack_node.meters as meters
    real = meters._read_host_mem
    meters._read_host_mem = lambda: (calls.append(1), real())[1]
    try:
        meters._HOST_MEM_MEMO.update(at=0.0, value=None)
        host_mem()
        host_mem()
        host_mem()
        assert len(calls) == 1
    finally:
        meters._read_host_mem = real


def test_a_platform_it_cannot_read_reports_nothing_rather_than_lying(monkeypatch):
    """Never raises: memory introspection breaking must not stop a node serving,
    and a zero would read as a machine with no memory."""
    import os

    import livestack_node.meters as meters

    def _no_sysconf(*_a):
        raise OSError("no such configuration variable")

    monkeypatch.setattr(sys, "platform", "sunos5")
    monkeypatch.setattr(os, "sysconf", _no_sysconf)
    monkeypatch.setitem(sys.modules, "psutil", None)   # import psutil -> raises
    assert meters._read_host_mem() is None
