from __future__ import annotations

import io
import json
import urllib.error

import pytest

from livestack_node.perception.contract import PerceptionContractError
from livestack_node.perception.remote import RemotePerceptionAdapter, matching_route
from livestack_node.perception.service import InferenceControl


ROUTES = [{"name": "mac", "url": "http://mac/v1/perception", "tokenFile": "/secret",
           "match": {"backends": ["mlx"], "models": ["mlx/model"]}}]


def test_remote_route_matches_backend_or_model_but_not_an_unconstrained_request():
    assert matching_route(ROUTES, {"backend": "mlx"})["name"] == "mac"
    assert matching_route(ROUTES, {"model": "mlx/model"})["name"] == "mac"
    assert matching_route(ROUTES, {}) is None
    assert matching_route(ROUTES, {"backend": "cuda"}) is None


def test_remote_adapter_preserves_identity_and_request(monkeypatch):
    seen = {}
    response = {"schemaVersion": "jingway.perception.v1", "requestId": "r", "outcome": "empty"}

    class Reply(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_): pass

    def open_(request, timeout):
        seen.update(headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
        return Reply(json.dumps(response).encode())

    monkeypatch.setattr("urllib.request.urlopen", open_)
    got = RemotePerceptionAdapter(url="http://mac/v1/perception", token="secret").infer(
        {"requestId": "r"}, grant={"owner": "histo", "realm": "research"})
    assert got == response
    assert seen["body"] == {"requestId": "r"}
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["headers"]["X-harmony-owner"] == "histo"
    assert seen["headers"]["X-harmony-realm"] == "research"


def test_remote_typed_error_is_preserved(monkeypatch):
    body = io.BytesIO(json.dumps({"cause": "unavailable", "detail": "capacity",
                                  "retryable": True}).encode())
    error = urllib.error.HTTPError("http://mac", 503, "no", {}, body)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    with pytest.raises(PerceptionContractError) as caught:
        RemotePerceptionAdapter(url="http://mac", token="secret").infer(
            {}, grant={"owner": "histo", "realm": "research"})
    assert caught.value.cause == "unavailable"
    assert caught.value.retryable is True


def test_remote_preserves_outer_deadline_cause(monkeypatch):
    body = io.BytesIO(json.dumps({"cause": "cancelled", "detail": "stopped",
                                  "retryable": False}).encode())
    error = urllib.error.HTTPError("http://mac", 408, "timeout", {}, body)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    control = InferenceControl(); control.cancel("deadline")
    with pytest.raises(PerceptionContractError) as caught:
        RemotePerceptionAdapter(url="http://mac", token="secret").infer(
            {"requestId": "r"}, grant={"owner": "histo", "realm": "research"},
            control=control)
    assert caught.value.cause == "deadline"
    assert caught.value.status == 408
