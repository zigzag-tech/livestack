"""Every imagegen entry point binds where LIVESTACK_LISTEN_HOST says.

A Harmony process-node facade runs the worker privately on loopback and refuses
a worker listening anywhere else (process_node_worker_must_listen_on_loopback).
"""
import importlib
import json

import pytest


@pytest.mark.parametrize("module, env", [
    ("livestack_node.imagegen.worker", "HARMONY_IMAGE_CONFIG"),
    ("livestack_node.imagegen.klein", "HARMONY_IMAGE_CONFIG"),
    ("livestack_node.imagegen.sensenova", "HARMONY_IMAGE_CONFIG"),
    ("livestack_node.imagegen.gateway", "HARMONY_IMAGE_GATEWAY_CONFIG"),
])
@pytest.mark.parametrize("listen, expected", [("127.0.0.1", "127.0.0.1"), (None, "0.0.0.0")])
def test_entry_point_binds_the_configured_listen_host(tmp_path, monkeypatch, module, env, listen, expected):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"port": 28216}))
    monkeypatch.setenv(env, str(config))
    if listen is None:
        monkeypatch.delenv("LIVESTACK_LISTEN_HOST", raising=False)
    else:
        monkeypatch.setenv("LIVESTACK_LISTEN_HOST", listen)
    target = importlib.import_module(module)
    worker = importlib.import_module("livestack_node.imagegen.worker")
    monkeypatch.setattr(target, "create_app", lambda *args, **kwargs: "app", raising=False)
    monkeypatch.setattr(worker, "create_app", lambda *args, **kwargs: "app")
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    target.main()
    assert calls == [{"host": expected, "port": 28216}]
