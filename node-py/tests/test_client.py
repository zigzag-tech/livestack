"""Consumer client: no-op when no endpoint; degrades gracefully when unreachable."""

from livestack_node import lease


def test_noop_when_no_base_url():
    with lease("diarize") as handle:
        assert handle.lease_id is None


def test_degrades_when_unreachable():
    with lease("diarize", base_url="http://127.0.0.1:1/livestack") as handle:
        assert handle.lease_id is None
