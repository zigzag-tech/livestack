"""The vLLM adapter drives `vllm serve` EXACTLY as server.py used to.

The fixture below is the pre-refactor argv for xc-tower-ubuntu's real
`llm_general` spec (copied from /etc/harmony/llm-units.json, adapters and all):
the engines seam may move the code, and may not change one byte of what gets
launched. Budget translation is pinned too — that arithmetic decides how much
of the card an engine claims.
"""
import json
import os
from pathlib import Path

import pytest

from engines import engine_for
from engines.vllm import DEFAULT_MAX_NUM_SEQS, SERVER_DIR, VllmEngine

ADAPTER_DIR = "/var/lib/harmony/adapters"

# /etc/harmony/llm-units.json on xc-tower-ubuntu, verbatim (the unit name and
# model are part of the launch line, so they belong in the fixture).
LLM_GENERAL = {
    "name": "llm_general",
    "model": "dbirks/Qwen3.8-27B-W4A16-AutoRound",
    "port": 8189,
    "footprint_gb": 21,
    "gpu_fraction": "0.96",
    "max_model_len": "24576",
    "extra_args": ["--max-num-seqs", "32", "--reasoning-parser", "qwen3",
                   "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml",
                   "--kv-cache-dtype", "fp8"],
    "residency": "UNPINNED",
    "attributes": {"class": "llm", "params_b": 27, "quant": "int4",
                   "family": "qwen", "vision": True, "context_len": 24576},
    "default": True,
    "warm_on_start": True,
    "adapters": {"chips-settinghead-v1": f"{ADAPTER_DIR}/chips-settinghead-v1",
                 "jemm": f"{ADAPTER_DIR}/jemm"},
    "lora_base": "Qwen/Qwen3.8-27B",
    "engine": "vllm",
    "engine_rev": "",
    "ram_gb": 0.0,
    "exclusive_device": False,
}

# The pre-refactor argv (server.py `_load`, 2026-10-02), captured as the
# fixture this adapter must reproduce byte for byte. Both deployed adapters
# declare rank 16, so `--max-lora-rank 16`.
PRE_REFACTOR_ARGV = [
    os.path.join(SERVER_DIR, "venv", "bin", "vllm"),
    "serve", "dbirks/Qwen3.8-27B-W4A16-AutoRound",
    "--port", "8189",
    "--host", "127.0.0.1",
    "--gpu-memory-utilization", "0.96",
    "--served-model-name", "dbirks/Qwen3.8-27B-W4A16-AutoRound",
    "llm_general", "local",
    "--scheduling-policy", "priority",
    "--max-model-len", "24576",
    "--max-num-seqs", "32", "--reasoning-parser", "qwen3",
    "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml",
    "--kv-cache-dtype", "fp8",
    "--enable-lora", "--max-loras", "2", "--max-lora-rank", "16",
    "--lora-modules",
    f"chips-settinghead-v1={ADAPTER_DIR}/chips-settinghead-v1",
    f"jemm={ADAPTER_DIR}/jemm",
]


def _real_spec():
    for name in ("chips-settinghead-v1", "jemm"):
        if not (Path(ADAPTER_DIR) / name / "adapter_config.json").exists():
            pytest.skip(f"deployed adapters not on this host ({ADAPTER_DIR})")
    return dict(LLM_GENERAL)


def test_llm_general_launches_exactly_as_it_always_did():
    spec = _real_spec()
    assert VllmEngine().argv(spec, None) == PRE_REFACTOR_ARGV


def test_the_default_engine_is_vllm():
    assert engine_for({**_real_spec(), "engine": ""}).name == "vllm"
    assert isinstance(engine_for(_real_spec()), VllmEngine)


def test_a_budget_scales_the_card_fraction_like_the_old_code():
    spec = _real_spec()
    total = 24 * (1 << 30)
    want = 12 * (1 << 30)
    argv = VllmEngine().argv(spec, {"vram_bytes": want}, total_bytes=total)
    # max(0.10, min(0.97, (want * 0.94) / total)) formatted to 3 places —
    # the exact arithmetic the old inline block did.
    expected = f"{max(0.10, min(0.97, (want * 0.94) / total)):.3f}"
    i = argv.index("--gpu-memory-utilization")
    assert argv[i + 1] == expected


def test_no_budget_keeps_the_declared_fraction():
    spec = _real_spec()
    argv = VllmEngine().argv(spec, None, total_bytes=24 * (1 << 30))
    i = argv.index("--gpu-memory-utilization")
    assert argv[i + 1] == "0.96"


def test_max_concurrent_comes_from_the_launch_line():
    from engines.vllm import max_concurrent
    assert max_concurrent(_real_spec()) == 32
    bare = {**_real_spec(), "extra_args": []}
    assert max_concurrent(bare) == DEFAULT_MAX_NUM_SEQS


def test_adapter_launch_args_are_the_vllm_flags():
    spec = _real_spec()
    args = VllmEngine().adapter_launch_args(spec)
    assert args[:4] == ["--enable-lora", "--max-loras", "2", "--max-lora-rank"]
    assert args[args.index("--lora-modules") + 1].startswith("chips-settinghead-v1=")


def test_an_unreadable_adapter_is_dropped_loudly(tmp_path, capsys):
    spec = {**_real_spec(), "adapters": {"ghost": str(tmp_path / "nope")}}
    assert VllmEngine().adapters(spec) == {}
    assert "NOT served" in capsys.readouterr().out
    # And therefore no attribute claims it — silence is not a yes.
    assert "adapter.ghost" not in VllmEngine().launch_attributes(spec)
