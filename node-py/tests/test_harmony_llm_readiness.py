"""A node reports what IT serves, not what answers on its ports.

`_vllm_up` asks the port. Where two nodes on one host read one units file
without distinct `HARMONY_LLM_PORT_OFFSET`s, the other node's engine answers —
and the node then advertises units it never started.

Measured on xc-tower-ubuntu 2026-09-23: `xc-tower-ubuntu-gpu0` ran no vLLM at
all and still reported `ready: true` / `serving llm_title, embed_multi`. It
sorts before `-gpu1`, won every `kind=llm` lookup, and proxied each request into
gpu1's engine through its own serialising hop — 0.9 s at concurrency 1, p50
7.5 s at 25, ~86% of the hub's classifier calls aborting on an 8 s deadline.
"""
import pathlib
import re

import pytest

SERVER = (pathlib.Path(__file__).resolve().parents[1]
          / "examples" / "harmony-llm" / "server.py")


def readiness_with(up, foreign, specs=("llm_title", "embed_multi")):
    """Run the real `_readiness` body against injected predicates."""
    src = SERVER.read_text()
    # Stop at the first line that starts in column 0: the function body is
    # indented, so that is exactly where `_readiness` ends.
    m = re.search(r"^def _readiness\(\).*?(?=^[^\s])", src, re.S | re.M)
    assert m, "_readiness not found"
    ns = {
        "SPECS": {n: {"model": f"model-of-{n}"} for n in specs},
        "MODEL": "fallback-model",
        "_vllm_up": lambda name=None: name in up,
        "_foreign_listener": lambda name: name in foreign,
    }
    exec(m.group(0), ns)
    return ns["_readiness"]()


def test_a_node_serving_its_own_units_is_ready():
    r = readiness_with(up={"llm_title"}, foreign=set())
    assert r["ready"] is True
    assert r["detail"] == "serving llm_title"
    assert r["model"] == "model-of-llm_title"


def test_a_node_whose_ports_are_answered_by_ANOTHER_node_is_not_ready():
    """The regression. Both ports answer; this node started neither."""
    r = readiness_with(up={"llm_title", "embed_multi"},
                       foreign={"llm_title", "embed_multi"})
    assert r["ready"] is False, "a proxy onto someone else's engine is not warm"
    assert "no unit resident" in r["detail"]


def test_it_NAMES_the_misconfiguration_rather_than_going_quiet():
    """One line fixes this, and the node is the only thing positioned to see it."""
    r = readiness_with(up={"llm_title", "embed_multi"},
                       foreign={"llm_title", "embed_multi"})
    assert "llm_title" in r["detail"] and "embed_multi" in r["detail"]
    assert "HARMONY_LLM_PORT_OFFSET" in r["detail"]


def test_a_node_with_nothing_loaded_is_cold_not_misconfigured():
    """Cold is normal and the fleet handles it: the unit loads on demand. It
    must not be reported as somebody else's engine."""
    r = readiness_with(up=set(), foreign=set())
    assert r["ready"] is False
    assert r["detail"] == "no unit resident"
    assert "PORT_OFFSET" not in r["detail"]


def test_its_own_units_still_count_when_a_sibling_holds_another():
    """Partial overlap must not blank a node that genuinely serves something."""
    r = readiness_with(up={"llm_title", "embed_multi"}, foreign={"embed_multi"})
    assert r["ready"] is True
    assert r["detail"] == "serving llm_title"
    assert "embed_multi" not in r["detail"]
