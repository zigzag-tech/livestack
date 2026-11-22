"""The node's own resident-reuse path must refuse generic demand to a
specialist-only unit, with the SAME list the planner uses. It kept its own copy,
("ocr",), which never learned `depth_guide`, `inpaint` or `trial`; on node-g
(2026-11-22) a `trial: true` Inkling unit then answered 19 production
`class=llm` requests (all failed) instead of flash_next."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    root = tmp_path_factory.mktemp("specialist")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "prod", "model": "m/prod", "port": 8189, "footprint_gb": 20,
         "attributes": {"class": "llm", "params_b": 125}},
        {"name": "candidate", "model": "m/candidate", "port": 8192, "footprint_gb": 20,
         "attributes": {"class": "llm", "params_b": 26.4, "trial": True}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_specialist", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_specialist"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_generic_demand_never_reaches_a_unit_on_trial(srv):
    assert srv._local_satisfies("prod", {"class": "llm"})
    assert not srv._local_satisfies("candidate", {"class": "llm"})
    assert not srv._local_satisfies("candidate", {"class": "llm", "params_b>=": 20})
    assert srv._local_satisfies("candidate", {"class": "llm", "trial": True})


def test_the_node_uses_the_planners_list():
    from livestack_node import planner
    src = HERE.read_text()
    assert "_SPECIALIST_ONLY_ATTRS" in src and '_LOCAL_SPECIALIST_ONLY = ("ocr",)' not in src
    assert "trial" in planner._SPECIALIST_ONLY_ATTRS
