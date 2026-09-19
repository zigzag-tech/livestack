"""Which unit answers a request — the question a caller must be able to trust.

Measured on xc-tower-ubuntu 2026-09-18: a request whose body said
`"model": "llm_title"` (a 27B) came back `200 OK` with
`"model": "cyankiwi/Qwen3.5-9B-AWQ-4bit"` in the response — a 9B — and
nothing in the exchange said a substitution had happened. Every chat request
derives `class=llm` from its PATH, that derived clause was ANDed into the
requirement unconditionally, and a non-None requirement meant the `model`
field was never read at all. The request was then resolved as "any unit of
class llm, preferring one already resident", and the 9B was the warm one.

These tests pin the two halves: what the caller NAMES is honoured, and what
the request IMPLIES is still derived when the caller named nothing.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    """The real module, with a units file of its own."""
    import json
    units = tmp_path_factory.mktemp("units") / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "twolven/Qwen3.8-27B-abliterated-AWQ-MTP",
         "port": 8189, "footprint_gb": 21,
         "attributes": {"class": "llm", "params_b": 27, "refusals": "abliterated",
                        "vision": True}},
        {"name": "llm_small", "model": "cyankiwi/Qwen3.5-9B-AWQ-4bit",
         "port": 8197, "footprint_gb": 12,
         "attributes": {"class": "llm", "params_b": 9}},
    ]))
    import os
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_a_named_unit_is_the_one_named(srv):
    assert srv._named_unit("llm_title") == "llm_title"
    assert srv._named_unit("twolven/Qwen3.8-27B-abliterated-AWQ-MTP") == "llm_title"


def test_naming_nothing_is_not_naming_the_first_one(srv):
    # The distinction the routing turns on: "no opinion" must be TELLABLE from
    # "asked for something", or a caller's choice cannot be honoured.
    assert srv._named_unit("local") is None
    assert srv._named_unit("") is None
    assert srv._named_unit("some-model-we-do-not-have") is None


def test_a_caller_with_no_opinion_still_gets_a_unit(srv):
    # The legacy path, unchanged: every existing caller sends `local`.
    assert srv._unit_for_model("local") == "llm_title"
    assert srv._unit_for_model("") == "llm_title"


def test_the_path_still_implies_a_class(srv):
    # The derivation itself is right and stays: an embeddings request must not
    # be routed to a generation unit.
    assert srv._derived_requirements("chat/completions", {})["class"] == "llm"
    assert srv._derived_requirements("embeddings", {})["class"] == "embed"


def test_an_image_still_implies_vision(srv):
    body = {"messages": [{"content": [{"type": "image_url", "image_url": {}}]}]}
    assert srv._derived_requirements("chat/completions", body).get("vision") is True


def test_the_named_unit_satisfies_what_the_path_implies(srv):
    # `llm_title` is class llm, so naming it in a chat request implies nothing
    # it cannot do — the check that decides whether a warning is printed.
    assert srv._local_satisfies("llm_title", {"class": "llm"})
    assert not srv._local_satisfies("llm_title", {"class": "embed"})


def test_an_undeclared_attribute_is_not_a_yes(srv):
    # Which is why naming a unit warns rather than refuses: a unit's attribute
    # list is routinely thinner than the unit.
    assert not srv._local_satisfies("llm_small", {"refusals": "abliterated"})


# --- what a unit actually occupies -----------------------------------------

def test_a_vllm_units_footprint_is_its_gpu_fraction_not_its_weights(srv, monkeypatch):
    """The planner must be told the size the unit really takes.

    A vLLM unit pre-allocates a KV cache up to `--gpu-memory-utilization`, so a
    27B at `gpu_fraction 0.96` holds 96% of the card whatever its weights
    weigh. Declaring the weights-shaped 21 GiB made the planner believe a
    23.6 GiB card had room for a second tenant; with one there, reconciled free
    went negative and rule 0 shed the biggest evictable unit — the 27B — whose
    SOFT_PIN reloaded it over the next two minutes, repeatedly.
    """
    monkeypatch.setattr(srv, "_device_total_bytes", lambda: 24.0 * (1 << 30))
    spec = {"footprint_gb": 21, "gpu_fraction": "0.96"}
    assert srv._footprint_bytes(spec) == int(24.0 * (1 << 30) * 0.96)


def test_the_larger_of_the_two_wins(srv, monkeypatch):
    # A unit whose declared footprint exceeds its fraction keeps the declared
    # one: understating is the failure, and nothing here should shrink a
    # number an operator raised on purpose.
    monkeypatch.setattr(srv, "_device_total_bytes", lambda: 24.0 * (1 << 30))
    assert srv._footprint_bytes({"footprint_gb": 23, "gpu_fraction": "0.5"}) == 23 * (1 << 30)


def test_no_card_no_guess(srv, monkeypatch):
    # On a host where the card cannot be read, the declaration is all there is.
    monkeypatch.setattr(srv, "_device_total_bytes", lambda: 0.0)
    assert srv._footprint_bytes({"footprint_gb": 21, "gpu_fraction": "0.96"}) == 21 * (1 << 30)


def test_a_missing_or_nonsense_fraction_is_ignored(srv, monkeypatch):
    monkeypatch.setattr(srv, "_device_total_bytes", lambda: 24.0 * (1 << 30))
    assert srv._footprint_bytes({"footprint_gb": 21}) == 21 * (1 << 30)
    assert srv._footprint_bytes({"footprint_gb": 21, "gpu_fraction": "nope"}) == 21 * (1 << 30)
    assert srv._footprint_bytes({"footprint_gb": 21, "gpu_fraction": "1.7"}) == 21 * (1 << 30)
