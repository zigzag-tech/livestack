"""`key=[a,b]` is "a value in this list" — in BOTH matchers.

The request language has spelled membership that way since the requirement
grammar existed (`require:class=llm,context_len=[131072,]` is the very shape
the harmony-engine-units scenarios use), and neither the planner's
`_unit_satisfies` nor the node's `_local_satisfies` implemented it: a list was
matched with `==`, so the clause satisfied NOTHING and the request came back
"no unit satisfies". The two matchers must agree about what a clause means —
one test file so they cannot drift apart again.
"""
from livestack_node.planner import Unit, _unit_satisfies


def _unit(attrs):
    return Unit("u", {"vram_bytes": 1}, attributes=dict(attrs))


def test_the_planner_treats_a_list_as_membership():
    u = _unit({"class": "llm", "context_len": 131072})
    assert _unit_satisfies(u, {"class": "llm", "context_len": [131072]})
    assert _unit_satisfies(u, {"context_len": [24576, 131072]})
    assert not _unit_satisfies(u, {"context_len": [24576]})
    # ...and `!=` with a list is "not one of these".
    assert _unit_satisfies(u, {"context_len!=": [24576]})
    assert not _unit_satisfies(u, {"context_len!=": [131072]})
    # A bare key is still equality; an undeclared attribute is still NOT met.
    assert _unit_satisfies(u, {"class": "llm"})
    assert not _unit_satisfies(u, {"nope": [1]})


def test_the_node_matcher_agrees_with_the_planner(tmp_path):
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"
    units = tmp_path / "units.json"
    units.write_text(json.dumps([{"name": "u", "model": "m/u", "port": 8189,
                                  "footprint_gb": 1, "attributes": {}}]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    os.environ.pop("HARMONY_LLM_UNITS", None)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_lists", here)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_lists"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # shared_py absent in some venvs
        import pytest
        pytest.skip(f"harmony-llm server not importable here: {e}")
    module.SPECS["u"] = {"name": "u", "attributes": {"class": "llm",
                                                     "context_len": 131072}}
    try:
        assert module._local_satisfies("u", {"class": "llm", "context_len": [131072]})
        assert module._local_satisfies("u", {"context_len": [24576, 131072]})
        assert not module._local_satisfies("u", {"context_len": [24576]})
        assert module._local_satisfies("u", {"context_len!=": [24576]})
        assert not module._local_satisfies("u", {"context_len!=": [131072]})
    finally:
        module.SPECS.pop("u", None)


def test_the_legacy_alias_local_is_a_named_choice(tmp_path):
    """`model: "local"` NAMES the default unit (harmony-engine-units scenario:
    "Named local still reaches the 27B ... never by flash_next") — it is not
    "no opinion", or a resident flash_next answers for the 27B through the
    reuse shortcut with nothing in the exchange saying so. Absent a `default`
    unit the alias keeps its old meaning: the first declared unit."""
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"
    units = tmp_path / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/27b", "port": 8189, "footprint_gb": 21,
         "default": True, "attributes": {"class": "llm", "context_len": 24576}},
        {"name": "flash_next", "model": "m/125b", "port": 8191, "footprint_gb": 22,
         "engine": "strata", "attributes": {"class": "llm", "context_len": 131072}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    os.environ.pop("HARMONY_LLM_UNITS", None)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_local", here)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_local"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        import pytest
        pytest.skip(f"harmony-llm server not importable here: {e}")
    try:
        # TWO answers, both needed: `_named_unit` stays strict ("does this name
        # a UNIT?" — no opinion must be TELLABLE), and the ROUTING treats the
        # alias as a choice through `_model_choice`.
        assert module._named_unit("local") is None
        assert module._model_choice({"model": "local"}) == "llm_title"
        assert module._unit_for_model("local") == "llm_title"
        assert module._named_unit("flash_next") == "flash_next"
        assert module._model_choice({"model": "flash_next"}) == "flash_next"
        assert module._model_choice({"model": "m/125b"}) == "flash_next"
        # A model that names nothing here is still "no opinion".
        assert module._model_choice({"model": "some-model-we-do-not-have"}) is None
        assert module._model_choice({"model": ""}) is None
    finally:
        pass
