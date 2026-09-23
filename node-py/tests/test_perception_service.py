from __future__ import annotations

import asyncio
import json

import httpx

from livestack_node.fleet_auth import load_principals
from livestack_node.perception.contract import PerceptionContractError, validate_request, validate_result
from livestack_node.perception.service import PerceptionService, build_app
from livestack_node.perception.admission import broker_admission_payload

TOKEN = "p" * 40
PRINCIPALS = load_principals(json.dumps({TOKEN: {"name": "histo", "owner": "histo"}}))


def request():
    return {
        "schemaVersion": "jingway.perception.v1",
        "requestId": "req-1",
        "images": [{
            "id": "image-1", "sha256": "a" * 64, "mediaType": "image/png",
            "width": 100, "height": 80, "objectRef": "sha256:" + "a" * 64,
            "sourceFromInput": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        }],
        "task": {"type": "grounding", "queryMode": "category",
                 "queries": [{"id": "q-1", "text": "building"}], "geometry": ["box"]},
        "requirements": {}, "limits": {"maxItems": 10},
    }


def result(req, *, x_max=20):
    return {
        "schemaVersion": "jingway.perception.v1", "requestId": req["requestId"], "outcome": "ok",
        "observations": [{"id": "o-1", "imageId": "image-1", "queryId": "q-1", "attributes": {},
                          "geometry": {"kind": "box", "box": {"xMin": 1, "yMin": 2, "xMax": x_max, "yMax": 30}}}],
        "unprocessed": [], "truncated": False,
        "execution": {"backend": "cuda", "device": "gpu0", "implementation": "fixture",
                      "model": "fixture", "modelRevision": "r1", "precision": "fp32",
                      "preprocessingRevision": "p1", "coldLoadMs": 0, "queueMs": 0,
                      "inferenceMs": 1, "totalMs": 1},
    }


class Backend:
    def __init__(self, x_max=20):
        self.calls = 0
        self.x_max = x_max

    def infer(self, req, *, grant, control=None):
        self.calls += 1
        return result(req, x_max=self.x_max)


def headers():
    return {"authorization": f"Bearer {TOKEN}", "x-harmony-realm": "research", "x-harmony-owner": "histo"}


def test_request_and_result_contract_reject_cross_reference_and_bounds():
    req = request()
    assert validate_request(req)["requestId"] == "req-1"
    try:
        validate_result(result(req, x_max=101), request=req)
        assert False, "out-of-bounds geometry accepted"
    except PerceptionContractError as exc:
        assert exc.cause == "invalid_output"


def test_service_authenticates_admits_dispatches_and_validates():
    backend = Backend()
    seen = []
    service = PerceptionService(
        principals=PRINCIPALS, backends={"cuda": backend},
        admit=lambda **kw: seen.append(kw) or {"backend": "cuda", "device": "gpu0"},
    )
    status, response = service.handle(request(), headers())
    assert status == 200, response
    assert backend.calls == 1
    assert seen[0]["owner"] == "histo"
    assert response["observations"][0]["geometry"]["kind"] == "box"


def test_missing_auth_and_direct_worker_never_run_inference():
    backend = Backend()
    service = PerceptionService(
        principals=PRINCIPALS, backends={"cuda": backend}, admit=lambda **_: {"backend": "cuda"},
    )
    assert service.handle(request(), {"x-harmony-realm": "research"})[0] == 401
    assert service.direct_worker(request())[0] == 403
    assert backend.calls == 0


def test_invalid_backend_output_is_a_typed_failure():
    backend = Backend(x_max=101)
    service = PerceptionService(
        principals=PRINCIPALS, backends={"cuda": backend}, admit=lambda **_: {"backend": "cuda"},
    )
    status, response = service.handle(request(), headers())
    assert status == 422
    assert response["cause"] == "invalid_output"


def test_auth_realm_never_becomes_a_hardware_selector():
    payload = broker_admission_payload("req", "histo")
    assert payload == {"id": "req", "kind": "ground_locateanything_nvidia_3b",
                       "owner": "histo", "owner_asserted": True}
    assert "selector" not in payload


def test_fastapi_treats_request_as_connection_state_not_query_parameter():
    backend = Backend()
    service = PerceptionService(
        principals=PRINCIPALS, backends={"cuda": backend},
        admit=lambda **_: {"backend": "cuda", "device": "gpu0"})

    async def post():
        transport = httpx.ASGITransport(app=build_app(service))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/perception", json=request(), headers=headers())

    response = asyncio.run(post())
    assert response.status_code == 200, response.text
    assert backend.calls == 1
