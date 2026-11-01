"""Real ingress refusal and contract controls; no model qualification is claimed."""
import copy
import hashlib

import pytest

from livestack_node.decisions.contract import ContractError, validate_request, validate_result
from livestack_node.decisions.cuda_adapter import CudaLayaAdapter
from livestack_node.decisions.mlx_adapter import MlxLayaAdapter
from livestack_node.decisions.extraction_contract import PROFILE_ID, validate_extraction
from livestack_node.decisions.fixture_backend import FixtureBackend
from livestack_node.decisions.paths import DATA_DIR
from test_decision_service import NOW, _body, _client, _headers, _svc


def extraction_request():
    request = _body()
    request["profile_id"] = PROFILE_ID
    request["questions"] = [{"id": "ask", "type": "extraction", "instructions": "Extract the existing question only."}]
    return request


def test_contract_content_receipt_binds_all_json_bytes():
    # HTTP cannot prove the frozen artifact covers every profile/schema/fixture.
    digest = hashlib.sha256()
    for path in sorted(DATA_DIR.rglob("*.json")):
        digest.update(path.relative_to(DATA_DIR).as_posix().encode())
        digest.update(b"\0" + path.read_bytes() + b"\0")
    assert (DATA_DIR / "HASH").read_text().strip() == digest.hexdigest()


def test_pinned_extraction_envelope_and_identity_constraints():
    request = extraction_request()
    assert validate_request(request, now_ms=NOW) is request
    for change in ({"profile_id": "pane-attention-v1:unqualified"},
                   {"questions": [{**request["questions"][0], "id": "other"}]},
                   {"questions": request["questions"] * 2}):
        with pytest.raises(ContractError):
            validate_request({**request, **change}, now_ms=NOW)


def test_real_ingress_refuses_unqualified_extraction_before_broker_or_worker():
    calls = []

    def broker(**kwargs):
        calls.append(kwargs)
        return {"granted": True, "lease_id": "lease-test"}

    service = _svc(broker=broker)
    client = _client(service)
    # Positive control verifies the broker instrument and authenticated route.
    assert client.post("/v1/decisions", json=_body(), headers=_headers()).status_code == 200
    assert len(calls) == 1
    response = client.post("/v1/decisions", json=extraction_request(), headers=_headers())
    assert response.status_code == 503
    assert response.json()["cause"] == "profile_unavailable"
    assert "qualification" in response.json()["detail"]
    assert len(calls) == 1
    assert service._cuda.calls + service._mlx.calls == 1


@pytest.mark.parametrize("adapter", [CudaLayaAdapter, MlxLayaAdapter])
def test_retired_adapter_refuses_extraction_without_loading_gpu_runtime(adapter):
    with pytest.raises(ContractError) as error:
        adapter().infer(extraction_request(), now_ms=NOW)
    assert error.value.cause == "profile_unavailable"
    assert "retired Laya" in error.value.detail


def test_structured_result_is_distinct_from_failure_and_requires_model_identity():
    result = FixtureBackend("cuda", "fixture").infer(_body(), now_ms=NOW)
    request = extraction_request()
    result["profile_id"] = PROFILE_ID
    result["answers"] = {"ask": {"type": "extraction", "value": {"outcome": "no_grounded_ask"}}}
    result["execution"]["qualification_hash"] = "fixture-contract-control"
    assert validate_result(result, request=request) is result
    for value in ("", "unqualified", None):
        invalid = copy.deepcopy(result)
        invalid["execution"]["qualification_hash"] = value
        with pytest.raises(ContractError):
            validate_result(invalid, request=request)


@pytest.mark.parametrize("value", [
    {"outcome": "no_grounded_ask", "question": "invented"},
    {"outcome": "ask", "kind": [], "request_quote": "Q?", "question": "Q?"},
    {"outcome": "ask", "kind": "open", "request_quote": "x" * 301, "question": "Q?"},
    {"outcome": "ask", "kind": "open", "request_quote": "Q?", "question": "Q?", "options": [{}] * 65},
])
def test_invalid_extractions_fail_by_name(value):
    with pytest.raises(ContractError) as error:
        validate_extraction(value)
    assert error.value.cause == "invalid_output"
