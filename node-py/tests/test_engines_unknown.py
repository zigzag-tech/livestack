"""An unknown engine is a STARTUP ERROR, naming itself.

A unit that names an engine this build does not have must fail when the units
file is read — not on its first request, and never silently (a unit that sits
in the catalogue looking loadable and then never serves is the slowest possible
failure)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from engines import ENGINES, engine_for

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


def test_engine_for_names_the_unknown_engine():
    with pytest.raises(ValueError) as e:
        engine_for({"name": "flash_next", "engine": "nonesuch"})
    assert "nonesuch" in str(e.value)
    assert "strata" in str(e.value) and "vllm" in str(e.value)


def test_the_known_engines_are_registered():
    assert {"vllm", "strata"} <= set(ENGINES)


def test_a_units_file_naming_an_unknown_engine_fails_at_startup(tmp_path, monkeypatch):
    units = tmp_path / "units.json"
    units.write_text(json.dumps([
        {"name": "flash_next", "model": "qwen/Q2_0", "engine": "nonesuch",
         "port": 8191, "footprint_gb": 22},
    ]))
    monkeypatch.setenv("HARMONY_LLM_UNITS_FILE", str(units))
    monkeypatch.delenv("HARMONY_LLM_UNITS", raising=False)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_badengine", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_badengine"] = module
    with pytest.raises(ValueError, match="nonesuch"):
        spec.loader.exec_module(module)
