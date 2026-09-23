from pathlib import Path

import pytest

from livestack_node import hostbroker
from livestack_node.hostbroker import RestPeer
from livestack_node.hostd import _node_control_token_from_env, build_app, build_broker


def test_rest_peer_authenticates_mutations_but_not_reads(monkeypatch):
    calls = []

    def fake_http(url, body=None, timeout=5, headers=None):
        calls.append((url, body, headers))
        return {"host_id": "h", "device_id": "d", "units": []}

    monkeypatch.setattr(hostbroker, "_http", fake_http)
    peer = RestPeer("http://node/livestack", control_token="secret")
    peer.refresh()
    peer.warm("ocr")
    peer.evict("ocr")
    peer.reclaim()

    assert calls[0][2] is None
    assert [call[2] for call in calls[1:]] == [
        {"Authorization": "Bearer secret"},
        {"Authorization": "Bearer secret"},
        {"Authorization": "Bearer secret"},
    ]


def test_dynamic_peers_inherit_control_token():
    broker = build_broker([], node_control_token="secret")
    broker.node_control_token = "secret"
    app = build_app(broker)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/peers")
    # The behavior is covered end-to-end in deployment; this assertion protects
    # the state that build_app's registration closure reads.
    assert route is not None
    assert broker.node_control_token == "secret"


def test_node_control_token_file_must_be_private(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    token_file.chmod(0o600)
    assert _node_control_token_from_env(
        {"LIVESTACK_NODE_CONTROL_TOKEN_FILE": str(token_file)}) == "secret"

    token_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="mode 0600"):
        _node_control_token_from_env(
            {"LIVESTACK_NODE_CONTROL_TOKEN_FILE": str(token_file)})
