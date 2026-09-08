"""The dashboard route and the one property that keeps it working everywhere."""
import pytest

from livestack_node.hostbroker import HostBroker
from livestack_node.ui import page

def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from livestack_node.hostd import build_app
    return TestClient(build_app(HostBroker(devices=[], peers=[])))


def test_the_broker_serves_the_map_at_the_root():
    r = _client().get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "HARMONY" in r.text


def test_the_page_is_self_contained():
    """Half this fleet is behind the GFW, where a page that pulls a framework
    from a CDN renders blank. No external origin, no build step: one file."""
    html = page()
    for bad in ("http://", "https://", "//cdn", "src=\"/"):
        assert bad not in html, f"the dashboard reaches for {bad!r}"


def test_it_offers_no_lever_only_a_view():
    """One card, one master. A page that could evict from a phone is a second
    one — and the fleet broker that serves it dispatches nothing at all."""
    html = page()
    assert "/model/evict" not in html and "/model/warm" not in html
    assert "method:\"POST\"" not in html.replace(" ", "")
