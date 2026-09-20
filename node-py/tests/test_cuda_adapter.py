"""Real CUDA infer on this host. CPU fallback is refused."""
from __future__ import annotations

import copy

import pytest

from livestack_node.decisions.contract import load_fixture
from livestack_node.decisions.cuda_adapter import CudaLayaAdapter
from livestack_node.decisions.mlx_adapter import MlxLayaAdapter

NOW = 1_700_000_000_000


def _req(name="status-en.json"):
    req = copy.deepcopy(load_fixture(name)["request"])
    req["deadline_at_ms"] = NOW + 15_000
    return req


def test_cuda_status_and_chip_requests_name_cuda_backend():
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("no CUDA device")
    adapter = CudaLayaAdapter(0)
    assert "NVIDIA" in adapter.device_name() or "GeForce" in adapter.device_name() or adapter.device_name()
    status = adapter.infer(_req(), now_ms=NOW)
    assert status["execution"]["backend"] == "cuda"
    assert set(status["answers"]) == {"attention"}
    p = status["answers"]["attention"]["probabilities"]
    assert abs(sum(p.values()) - 1) <= 1e-3
    assert all(0 <= v <= 1 for v in p.values())

    chips = copy.deepcopy(_req())
    chips["profile_id"] = "pane-chips-v1:unqualified"
    from livestack_node.decisions.identity import candidate_id
    from livestack_node.decisions.packing import noul_question
    from livestack_node.decisions.contract import load_profile
    template = load_profile("pane-chips-v1.json")["templates"]["noul_instructions"]
    qids = []
    questions = []
    for reply in ["git status", "use Postgres"]:
        qid = candidate_id("acct_dev", reply)
        qids.append(qid)
        questions.append(noul_question(qid, reply, template))
    chips["questions"] = questions
    out = adapter.infer(chips, now_ms=NOW)
    assert set(out["answers"]) == set(qids)
    for ans in out["answers"].values():
        assert ans["type"] == "noul"
        assert 0 <= ans["probability"] <= 1
    assert out["execution"]["backend"] == "cuda"


def test_mlx_adapter_does_not_import_torch_at_module_load():
    import livestack_node.decisions.mlx_adapter as mod
    import inspect
    src = inspect.getsource(mod)
    assert "\nimport torch" not in src and not src.startswith("import torch")
    assert "\nimport transformers" not in src
    # Loading the module must not pull mlx either if absent; infer() does.
    adapter = MlxLayaAdapter()
    from livestack_node.decisions.contract import ContractError
    with pytest.raises(ContractError, match="mlx"):
        adapter.infer(_req(), now_ms=NOW)
