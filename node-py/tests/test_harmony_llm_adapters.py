"""A LoRA adapter is something a unit SERVES, stated in the request language.

The case this exists for: one 3090 holds one 27B, and two callers need it --
the typed-decision classifier on the base weights, and chip generation on a
LoRA. vLLM serves both from one engine when started with --enable-lora; the
adapter is chosen per request by the model name. So Harmony must (1) start the
engine with the flags derived from the adapters actually present, (2) publish
one attribute per adapter so `adapter=<name>` selects that unit, and (3) send
the adapter's name as `model`, or the caller silently gets the base weights.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("adapters")
    good = root / "chips-v1"; good.mkdir()
    (good / "adapter_config.json").write_text(json.dumps({"r": 16, "peft_type": "LORA"}))
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "dbirks/Qwen3.8-27B-W4A16-AutoRound", "port": 8189,
         "footprint_gb": 21, "attributes": {"class": "llm", "params_b": 27, "family": "qwen"},
         "adapters": {"chips-v1": str(good), "broken": str(root / "missing")}},
        {"name": "llm_small", "model": "cyankiwi/Qwen3.5-4B-AWQ-4bit", "port": 8201,
         "footprint_gb": 7, "attributes": {"class": "llm", "params_b": 4, "family": "qwen"}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_adapters", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_adapters"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    module._good_path = str(good)
    return module


def test_launch_flags_come_from_the_adapters_that_exist(srv):
    args = srv._lora_launch_args(srv.SPECS["llm_title"])
    assert args == ["--enable-lora", "--max-loras", "1", "--max-lora-rank", "16",
                    "--lora-modules", f"chips-v1={srv._good_path}"]


def test_a_unit_without_adapters_gets_no_lora_flags(srv):
    # --enable-lora costs every batch on that engine; a unit with nothing to
    # serve through it must not pay.
    assert srv._lora_launch_args(srv.SPECS["llm_small"]) == []


def test_an_unreadable_adapter_is_not_advertised(srv):
    attrs = srv.SPECS["llm_title"]["attributes"]
    assert attrs.get("adapter.chips-v1") is True
    assert "adapter.broken" not in attrs


def test_the_clause_selects_only_the_unit_serving_the_adapter(srv):
    req = srv._requirement_from({"model": "require:class=llm,family=qwen,adapter=chips-v1"})
    assert req["adapter.chips-v1"] is True
    assert srv._local_satisfies("llm_title", req)
    assert not srv._local_satisfies("llm_small", req)


def test_asking_for_an_adapter_nobody_serves_matches_nothing(srv):
    # Silence is not a yes: a missing adapter must not fall back to base weights.
    req = srv._requirement_from({"model": "require:class=llm,adapter=broken"})
    assert not any(srv._local_satisfies(n, req) for n in srv.SPECS)


def test_callers_that_do_not_ask_for_an_adapter_are_unchanged(srv):
    # The typed-decision classifier's requirement, verbatim: it must still land
    # on the 27B and be sent the BASE model, not the adapter.
    req = srv._requirement_from({"model": "require:class=llm,family=qwen,params_b=[20,30)"})
    assert srv._local_satisfies("llm_title", req)
    assert not any(k.startswith("adapter.") for k in req)


def test_an_empty_adapter_clause_is_refused(srv):
    with pytest.raises(Exception):
        srv._requirement_from({"model": "require:class=llm,adapter="})


def test_an_adapter_named_as_the_model_is_asked_for_by_requirement(srv):
    # The forwarding hop. A node that resolves `adapter=chips-v1` and forwards
    # to the peer holding the unit sends `model: "chips-v1"` -- the name vLLM
    # selects a LoRA by. The peer must read that as the adapter requirement;
    # treating it as an unknown name resolved "any llm" and rewrote `model` to
    # the BASE, so the caller got base weights and a 200 (seen 2026-09-22).
    req = srv._requirement_from({"model": "chips-v1"})
    assert req == {"adapter.chips-v1": True}
    assert srv._local_satisfies("llm_title", req)
    assert not srv._local_satisfies("llm_small", req)


def test_an_unknown_model_name_is_still_not_a_requirement(srv):
    assert srv._requirement_from({"model": "some-model-we-do-not-have"}) is None
    assert srv._requirement_from({"model": "llm_title"}) is None
