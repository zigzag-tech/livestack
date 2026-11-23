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


def test_a_unified_pool_is_the_declared_window_not_one_per_slot():
    """--kv-unified: every slot may use the whole -c, so multiplying it would
    allocate twice the KV the unit declared (and not fit the card)."""
    argv = LlamaCppEngine().argv(dict(SPEC, extra_args="--jinja --kv-unified"))
    assert argv[argv.index("-np") + 1] == "2"
    assert argv[argv.index("-c") + 1] == "32768"
    assert LlamaCppEngine().launch_attributes(dict(SPEC, extra_args="--kv-unified"))["context_len"] == 32768


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


def _body(**kw):
    import json as _json
    base = {"model": "inkling_mini", "messages": [{"role": "user", "content": "x"},
                                                  {"role": "assistant", "content": '{"answer": "'}],
            "continue_final_message": True, "max_tokens": 1, "logprobs": True, "top_logprobs": 20,
            "structured_outputs": {"choice": ["A", "B", "C", "D", "E", "F"]}}
    base.update(kw)
    return _json.dumps(base).encode()


def test_a_choice_widens_top_logprobs_so_every_label_is_scored():
    """llama.cpp ignores vLLM's structured_outputs and reports top_logprobs from the
    raw logits; at 20 the pane-status labels came back 2 of 6 (Simple Jev then refuses
    the answer). Widening the top-k is the only lever, and it is cheap (~80 ms at 1000)."""
    import json as _json
    from engines.llamacpp import CHOICE_TOP_LOGPROBS
    out = _json.loads(LlamaCppEngine().prepare_request(SPEC, "chat/completions", _body()))
    assert out["top_logprobs"] == CHOICE_TOP_LOGPROBS >= 1000
    assert "structured_outputs" not in out
    assert out["continue_final_message"] is True and out["max_tokens"] == 1


def test_a_larger_ask_and_requests_without_a_choice_pass_untouched():
    import json as _json
    e = LlamaCppEngine()
    wide = _json.loads(e.prepare_request(SPEC, "chat/completions", _body(top_logprobs=5000)))
    assert wide["top_logprobs"] == 5000
    plain = _body()
    plain_obj = _json.loads(plain); plain_obj.pop("structured_outputs")
    raw = _json.dumps(plain_obj).encode()
    assert e.prepare_request(SPEC, "chat/completions", raw) == raw
    no_lp = _json.loads(_body(logprobs=False))
    assert "top_logprobs" in no_lp and _json.loads(
        e.prepare_request(SPEC, "chat/completions", _body(logprobs=False)))["top_logprobs"] == 20
    assert e.prepare_request(SPEC, "embeddings", b"not json") == b"not json"


def test_other_engines_send_the_body_as_it_is():
    from engines.vllm import VllmEngine
    from engines.strata import StrataEngine
    for e in (VllmEngine(), StrataEngine()):
        assert e.prepare_request({}, "chat/completions", _body()) == _body()
