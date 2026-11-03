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


@pytest.fixture(autouse=True)
def _hermetic_host(monkeypatch):
    """The launch line must not depend on the machine running the test: no CUDA
    free figure (a real card would clamp the fraction) and an interpreter with no
    vllm beside it (a real venv would move argv[0])."""
    monkeypatch.setattr("engines.vllm.device_free_bytes", lambda: 0.0)
    monkeypatch.setattr("sys.executable", "/nonexistent/bin/python")

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


# -- sizing a start nobody planned (boot warm-up) and finding the binary -------

_MIN_SPEC = {"name": "u", "model": "m/x", "port": 8189, "gpu_fraction": "0.96",
             "max_model_len": "", "extra_args": []}
_GIB = 1 << 30


def _frac(argv):
    return argv[argv.index("--gpu-memory-utilization") + 1]


def test_an_ungranted_start_never_claims_more_than_the_card_has_free(monkeypatch):
    # 2026-10-19: 0.96 of a 23.6 GiB card with 21.2 GiB free; vLLM exited 4x.
    monkeypatch.setattr("engines.vllm.device_free_bytes", lambda: 21.2 * _GIB)
    got = float(_frac(VllmEngine().argv(_MIN_SPEC, None, total_bytes=23.56 * _GIB)))
    assert got == pytest.approx(21.2 * 0.94 / 23.56, abs=1e-3)
    assert got < 0.96


def test_an_ungranted_start_keeps_a_fraction_already_below_free(monkeypatch):
    monkeypatch.setattr("engines.vllm.device_free_bytes", lambda: 23 * _GIB)
    spec = {**_MIN_SPEC, "gpu_fraction": "0.50"}
    assert _frac(VllmEngine().argv(spec, None, total_bytes=24 * _GIB)) == "0.50"


def test_an_unknowable_free_figure_keeps_the_configured_fraction(monkeypatch):
    monkeypatch.setattr("engines.vllm.device_free_bytes", lambda: 0.0)
    assert _frac(VllmEngine().argv(_MIN_SPEC, None, total_bytes=24 * _GIB)) == "0.96"


def test_vllm_is_found_beside_the_interpreter_when_the_server_dir_has_no_venv(
        tmp_path, monkeypatch):
    import engines.vllm as ev
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "vllm").write_text("#!/bin/sh\n")
    monkeypatch.setattr(ev, "SERVER_DIR", str(tmp_path / "release"))
    monkeypatch.setattr(ev.sys, "executable", str(bindir / "python"))
    assert ev.vllm_bin() == str(bindir / "vllm")
    monkeypatch.setattr(ev.sys, "executable", str(tmp_path / "nowhere" / "python"))
    assert ev.vllm_bin() == os.path.join(str(tmp_path / "release"), "venv", "bin", "vllm")


def test_a_grant_never_raises_the_fraction_above_the_configured_one(monkeypatch):
    """A 3 GiB embedder granted a whole card was launched at 0.94 of it and
    vLLM refused (rc=1) while any other tenant held memory."""
    import livestack_node  # noqa: F401
    from importlib import import_module
    mod = import_module(VllmEngine.__module__)
    monkeypatch.setattr(mod, "device_free_bytes", lambda: 0.0)
    spec = {**_real_spec(), "gpu_fraction": "0.12"}
    total = 24 * (1 << 30)
    argv = VllmEngine().argv(spec, {"vram_bytes": 23 * (1 << 30)}, total_bytes=total)
    assert argv[argv.index("--gpu-memory-utilization") + 1] == "0.120"


def test_a_grant_is_clamped_to_live_free_vram(monkeypatch):
    from importlib import import_module
    mod = import_module(VllmEngine.__module__)
    total = 24 * (1 << 30)
    monkeypatch.setattr(mod, "device_free_bytes", lambda: 20.8 * (1 << 30))
    argv = VllmEngine().argv(_real_spec(), {"vram_bytes": 23 * (1 << 30)}, total_bytes=total)
    assert argv[argv.index("--gpu-memory-utilization") + 1] == f"{(20.8 * 0.94) / 24:.3f}"
