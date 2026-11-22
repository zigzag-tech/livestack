"""engines/llamacpp.py: the launch line, readiness and what the line proves."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"))

import pytest  # noqa: E402

from engines import engine_for  # noqa: E402
from engines.llamacpp import LlamaCppEngine  # noqa: E402

SPEC = {"name": "inkling_mini", "engine": "llamacpp", "port": 8193,
        "llamacpp_bin": "/opt/llama/llama-server", "gguf": "/m/Inkling-2-Mini-UD-Q4_K_XL.gguf",
        "mmproj": "", "max_model_len": "32768",
        "extra_args": "--jinja --flash-attn on",
        "attributes": {"class": "llm", "max_concurrent": 2, "thinking": True, "tools": True}}


def test_the_units_file_selects_it():
    assert isinstance(engine_for(SPEC), LlamaCppEngine)


def test_the_launch_line_gives_each_slot_the_declared_window():
    argv = LlamaCppEngine().argv(SPEC)
    assert argv[0] == "/opt/llama/llama-server"
    assert argv[argv.index("-m") + 1] == SPEC["gguf"]
    assert argv[argv.index("-np") + 1] == "2"
    assert argv[argv.index("-c") + 1] == "65536"      # 2 slots x 32768
    assert argv[argv.index("--port") + 1] == "8193" and "127.0.0.1" in argv
    assert argv[-3:] == ["--jinja", "--flash-attn", "on"] and "--mmproj" not in argv


def test_a_unit_without_binary_or_model_is_refused():
    with pytest.raises(ValueError):
        LlamaCppEngine().argv(dict(SPEC, gguf=""))


def test_vision_only_with_a_projector_on_disk(tmp_path):
    e = LlamaCppEngine()
    assert e.launch_attributes(SPEC)["vision"] is False
    proj = tmp_path / "mmproj.gguf"
    proj.write_bytes(b"x")
    spec = dict(SPEC, mmproj=str(proj))
    assert e.launch_attributes(spec)["vision"] is True
    assert "--mmproj" in e.argv(spec)


def test_thinking_and_tools_need_the_models_own_template():
    e = LlamaCppEngine()
    a = e.launch_attributes(SPEC)
    assert a["thinking"] is True and a["tools"] is True and a["context_len"] == 32768
    b = e.launch_attributes(dict(SPEC, extra_args="--flash-attn on"))
    assert b["thinking"] is False and b["tools"] is False
