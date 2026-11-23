"""The llama.cpp engine: one `llama-server` per unit, serving a GGUF.

For a model whose official checkpoint our cards cannot run (Inkling 2 Mini's
NVFP4/MXFP8 kernels need compute capability 9.0; vLLM refuses it on a 3090 or
4080) but whose GGUF quantizations llama.cpp serves on any CUDA card. The
release is upstream's prebuilt `llama-b<N>-bin-ubuntu-cuda-12.8-x64` tarball;
the unit names its `llama-server`.

Units file fields (read here, ignored by the other engines):

* `llamacpp_bin` — the `llama-server` executable (required);
* `gguf` — the model file (required); `mmproj` — the vision projector, optional:
  `vision` is true exactly when that file exists;
* `max_model_len` -> `-c`, `attributes.max_concurrent` -> `-np` (llama.cpp
  splits `-c` across its slots, so the per-request window is `-c / -np`: the
  adapter multiplies, so the declared `context_len` is what each request gets).
  With `--kv-unified` in `extra_args` the slots share ONE pool of `-c` and any
  request may fill it, so `-c` is the declared window, not multiplied: two
  slots at 131072 then cost what one does (Inkling 2 Mini Q4 on a 3090:
  22.2 GB either way, 24.4 GB as two separate pools), and two requests whose
  contexts together exceed the pool contend for it;
* `extra_args` — anything else (`--flash-attn on`, `--cache-type-k q8_0`, ...).

The weights are loaded up front at a fixed size (`-ngl 999`), so a planner
grant does not resize it: the unit's declared footprint is its size, and the
NVML reading of the process confirms or corrects it.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Mapping, Optional

import httpx

from . import Engine, terminate_and_wait
from .strata import StrataEngine

DEFAULT_MAX_CONCURRENT = 1

# The top-k a scored choice is asked at. vLLM's `structured_outputs.choice` forces
# every permitted label into the returned support; llama.cpp ignores it and reports
# `top_logprobs` from the raw logits (a grammar, `logit_bias` or
# `post_sampling_probs` does not change them). At 20 the pane-status labels came
# back 2 of 6 on Inkling 2 Mini, which Simple Jev refuses; at 1000 all six were
# there (lowest -20.6), at ~80 ms (llama-server has no top-20 cap). Measured on
# node-h, 2026-11-23 (xc-setup docs/inkling-2-mini-harmony.md).
CHOICE_TOP_LOGPROBS = 1000


def _tokens(spec: dict) -> "list[str]":
    args = spec.get("extra_args")
    return list(args) if isinstance(args, list) else shlex.split(str(args or ""))


def _slots(spec: dict) -> int:
    try:
        n = int((spec.get("attributes") or {}).get("max_concurrent") or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else DEFAULT_MAX_CONCURRENT


class LlamaCppEngine(Engine):
    name = "llamacpp"

    def argv(self, spec: dict, budget: "Mapping[str, float] | None" = None,
             total_bytes: Optional[float] = None) -> "list[str]":
        binary = str(spec.get("llamacpp_bin") or "").strip()
        gguf = str(spec.get("gguf") or "").strip()
        if not binary or not gguf:
            raise ValueError(f"unit {spec.get('name')!r}: a llamacpp unit needs "
                             f"`llamacpp_bin` and `gguf`")
        slots = _slots(spec)
        cmd = [binary, "-m", gguf, "--host", "127.0.0.1", "--port", str(spec["port"]),
               "--alias", str(spec["name"]), "-ngl", "999", "-np", str(slots)]
        if spec.get("max_model_len"):
            pool = 1 if "--kv-unified" in _tokens(spec) else slots
            cmd += ["-c", str(int(spec["max_model_len"]) * pool)]
        mmproj = str(spec.get("mmproj") or "").strip()
        if mmproj:
            cmd += ["--mmproj", mmproj]
        return cmd + _tokens(spec)

    def ready(self, spec: dict, timeout: float = 2.0) -> bool:
        """`/health` is 503 while the model loads and 200 once it serves."""
        try:
            r = httpx.get(f"http://127.0.0.1:{spec['port']}/health", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def measure(self, spec: dict, capture, pid: "int | None",
                argv: "list[str]") -> Optional[dict]:
        family = StrataEngine._family(pid)
        vram = StrataEngine._pid_vram_bytes(family)
        row: dict = {"measured": "unknown", "source": "llamacpp-startup",
                     "unmatched": ["no NVML reading for the llama-server process"]}
        anon = mapped = None
        for p in family:
            _, fil, ano = StrataEngine._proc_status_bytes(p, anon=True)
            mapped = (mapped or 0) + fil if fil is not None else mapped
            anon = (anon or 0) + ano if ano is not None else anon
        if anon is not None:
            row["ram_resident_bytes"] = anon
        if mapped is not None:
            row["ram_mapped_bytes"] = mapped
        if vram is not None:
            row.update({"measured": "llamacpp-startup", "footprint": vram,
                        "min_footprint": vram, "unmatched": []})
        return row

    def launch_attributes(self, spec: dict) -> dict:
        """Declared attributes, corrected by what the launch line proves:
        `vision` (the projector exists), `max_concurrent` (the slots it runs)
        and `context_len` (what each slot gets). `thinking` needs `--jinja`:
        without the model's own template llama.cpp cannot split the reasoning
        out of `content`, and a unit claiming it would leak narration."""
        attrs = dict(spec.get("attributes") or {})
        attrs["class"] = "llm"
        toks = _tokens(spec)
        attrs["thinking"] = bool(attrs.get("thinking", False)) and "--jinja" in toks
        attrs["tools"] = bool(attrs.get("tools", False)) and "--jinja" in toks
        mmproj = str(spec.get("mmproj") or "").strip()
        attrs["vision"] = bool(mmproj) and os.path.exists(mmproj)
        attrs["max_concurrent"] = _slots(spec)
        if spec.get("max_model_len"):
            attrs["context_len"] = int(spec["max_model_len"])
        return attrs

    def prepare_request(self, spec: dict, path: str, body: bytes) -> bytes:
        """`structured_outputs.choice` with `logprobs`: widen `top_logprobs` so the
        labels are scored, and drop the field llama.cpp does not know. Anything
        else passes byte for byte."""
        if not path.endswith("completions"):
            return body
        try:
            req = json.loads(body)
        except ValueError:
            return body
        so = req.get("structured_outputs") if isinstance(req, dict) else None
        if not (isinstance(so, dict) and so.get("choice") and req.get("logprobs")):
            return body
        req.pop("structured_outputs")
        try:
            asked = int(req.get("top_logprobs") or 0)
        except (TypeError, ValueError):
            asked = 0
        req["top_logprobs"] = max(asked, CHOICE_TOP_LOGPROBS)
        return json.dumps(req).encode()

    def stop(self, proc) -> None:
        terminate_and_wait(proc, "llama-server")

    def context_refusal(self, status: int, body: bytes) -> Optional[str]:
        if status not in (400, 413, 500):
            return None
        text = body.decode("utf-8", "replace").lower()
        if "context" in text and ("exceed" in text or "too long" in text or "size" in text):
            return body.decode("utf-8", "replace")
        return None

    def _version_file(self, spec: dict) -> str:
        """The release directory name (`llama-b12633-...`), when the binary
        lives in one; "" otherwise."""
        binary = str(spec.get("llamacpp_bin") or "")
        for part in reversed(os.path.realpath(binary).split(os.sep)):
            if part.startswith("llama-b") or (part.startswith("b") and part[1:].isdigit()):
                return part
        return ""
