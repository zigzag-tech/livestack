"""A unit's `decision_heads` reach the classifier and the fleet: the units file
names them, the unit advertises `head.<name>`, and `head=<name>` selects it."""
import importlib.util
import json
import os

import pytest

from engines.strata import StrataEngine
from request_language import expand_clause


def test_head_clause_is_one_boolean_attribute():
    assert expand_clause("head", "pane-status-v1") == [("head.pane-status-v1", True)]
    with pytest.raises(ValueError):
        expand_clause("head", " ")


def test_a_strata_unit_advertises_the_heads_it_registers():
    attrs = StrataEngine().launch_attributes({
        "name": "u", "model": "m", "strata_root": "/nonexistent",
        "attributes": {"head.stale": True},
        "decision_heads": {"pane-status-v1": "/var/lib/harmony/heads/p.npz"}})
    assert attrs["head.pane-status-v1"] is True
    assert "head.stale" not in attrs          # declared, not registered: a lie


def test_the_units_file_registers_heads_with_the_classifier(monkeypatch):
    pytest.importorskip("fastapi")
    units = [{"name": "flash_next", "model": "m", "port": 8199, "engine": "strata",
              "strata_root": "/nonexistent",
              "decision_heads": {"pane-status-v1": "/var/lib/harmony/heads/p.npz"}}]
    monkeypatch.setenv("HARMONY_LLM_UNITS", json.dumps(units))
    monkeypatch.setenv("HARMONY_LLM_WARM_ON_START", "0")
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_ACT_SAMPLE_S", "0")
    monkeypatch.delenv("HARMONY_LLM_UNITS_FILE", raising=False)
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "harmony-llm", "server.py")
    spec = importlib.util.spec_from_file_location("harmony_llm_heads_under_test", os.path.abspath(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._decision_heads() == {"pane-status-v1": "/var/lib/harmony/heads/p.npz"}
    assert mod.SPECS["flash_next"]["attributes"]["head.pane-status-v1"] is True
