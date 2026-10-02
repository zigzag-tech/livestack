"""The vLLM engine: one `vllm serve` subprocess per unit, shaped by its argv.

This is the code that used to live in server.py — moved verbatim in behaviour
(see `tests/test_engines_vllm.py`, which pins the launch line for
xc-tower-ubuntu's real `llm_general` spec against the pre-refactor argv). The
first non-vLLM engine is the reason it moved: Strata launches a
llama.cpp-lineage binary whose weights live in host RAM and whose cache sizes
itself to the free VRAM, and the seam between "how an engine is driven" and
"how requests are routed" had never been drawn.
"""
from __future__ import annotations

import json
import os
import shlex
import time
from typing import Mapping, Optional

import httpx

from . import Engine, LineCapture, terminate_and_wait

# The harmony-llm directory (the parent of this package), where the deployment
# keeps its venv — `venv/bin/vllm`.
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# vLLM's documented default for `--max-num-seqs` (256 since v0.5). A unit that
# names the flag gets its number; one that does not gets this, because that IS
# what the engine admits.
DEFAULT_MAX_NUM_SEQS = 256


def is_pooling(joined: str) -> bool:
    """Was this unit started to POOL (embed) rather than GENERATE?

    Two spellings because vLLM renamed the flag: `--task embed` through v0.9,
    `--runner pooling` from v0.10. Both are matched so a node is not silently
    misrouted by a vLLM upgrade.
    """
    return "--task embed" in joined or "--runner pooling" in joined


def adapters_for(spec: dict) -> "dict[str, tuple[str, int]]":
    """The LoRA adapters a unit is started with: name -> (path, rank).

    Declared in the units file as ``"adapters": {"chips-v1": "/path/to/adapter"}``.
    The rank is READ from the adapter's own ``adapter_config.json`` rather than
    declared, for the same reason the other launch-line facts are derived: a
    hand-typed ``--max-lora-rank`` below an adapter's real rank makes vLLM refuse
    it at load, and one above it wastes the card's memory on every batch.

    An adapter whose config cannot be read is DROPPED, loudly, and therefore has
    no attribute: a request that names it gets 503 "nothing satisfies", never a
    silent answer from the base model it was trying not to be.
    """
    out: "dict[str, tuple[str, int]]" = {}
    for name, path in sorted((spec.get("adapters") or {}).items()):
        try:
            with open(os.path.join(str(path), "adapter_config.json"), "r", encoding="utf-8") as fh:
                rank = int(json.load(fh)["r"])
        except Exception as e:  # noqa: BLE001 -- any unreadable adapter is excluded
            print(f"[harmony-llm] unit {spec.get('name')}: adapter {name!r} at {path} "
                  f"is NOT served ({type(e).__name__}: {e})", flush=True)
            continue
        out[str(name)] = (str(path), rank)
    return out


def lora_launch_args(spec: dict) -> "list[str]":
    """The vLLM flags that serve a unit's adapters beside its base model.

    One engine answers both: a request naming the base model gets the base
    weights and one naming an adapter gets base + adapter, in the SAME batch.
    That is the whole point on a single card -- the typed-decision classifier
    and a chip adapter share one resident 27B instead of needing two.
    """
    adapters = adapters_for(spec)
    if not adapters:
        return []
    return (["--enable-lora", "--max-loras", str(len(adapters)),
             "--max-lora-rank", str(max(r for _, r in adapters.values())),
             "--lora-modules"] + [f"{n}={p}" for n, (p, _) in adapters.items()])


def _flag_value(spec: dict, flag: str) -> str:
    """The value a launch-line flag carries in this unit's extra_args, or ''."""
    args = spec.get("extra_args")
    argv = args if isinstance(args, list) else shlex.split(str(args or ""))
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return ""


def max_concurrent(spec: dict) -> int:
    """Concurrent requests this launch admits: `--max-num-seqs`, or vLLM's own
    default when the flag is absent (it is the engine's scheduler limit, and
    the unit's queue exists to stop requests piling into an engine already at
    it)."""
    try:
        n = int(_flag_value(spec, "--max-num-seqs"))
    except ValueError:
        n = 0
    return n if n > 0 else DEFAULT_MAX_NUM_SEQS


def device_total_bytes() -> float:
    """Total VRAM of the card this node speaks for, or 0 when unknowable."""
    try:
        import torch
        if torch.cuda.is_available():
            return float(torch.cuda.get_device_properties(0).total_memory)
    except Exception:
        pass
    return 0.0


class VllmEngine(Engine):
    name = "vllm"

    # -- what to launch ----------------------------------------------------
    def argv(self, spec: dict, budget: "Mapping[str, float] | None" = None,
             total_bytes: Optional[float] = None) -> "list[str]":
        # SIZE TO THE BUDGET THE PLANNER GRANTED, when it gave one.
        #
        # `gpu_fraction` is a fraction of the WHOLE card, fixed in config, and it
        # cannot know what else is on that card or what the planner just evicted.
        # Tuning it by hand to squeeze a model into one card's leftovers is
        # placement decided by an operator again, and it is wrong the moment the
        # card's other tenants change. The planner knows the free bytes; use them.
        fraction = spec["gpu_fraction"]
        want = float((budget or {}).get("vram_bytes") or 0)
        if want > 0:
            total = float(total_bytes or 0) or device_total_bytes()
            if total > 0:
                # Leave the tail of the grant unclaimed: the budget is what is
                # free, and an engine that takes every last byte leaves nothing
                # for the allocator's own overhead.
                fraction = f"{max(0.10, min(0.97, (want * 0.94) / total)):.3f}"
                print(f"[harmony-llm] {spec['name']}: planner granted "
                      f"{want/(1<<30):.1f} GiB -> --gpu-memory-utilization {fraction}",
                      flush=True)
        cmd = [
            os.path.join(SERVER_DIR, "venv", "bin", "vllm"),
            "serve", spec["model"],
            "--port", str(spec["port"]),
            "--host", "127.0.0.1",
            "--gpu-memory-utilization", fraction,
            "--served-model-name", spec["model"], spec["name"], "local",
        ]
        # Unit priority decides which model may occupy a device. Once callers
        # share a resident vLLM, request priority is a separate queueing concern.
        # Enable vLLM's native scheduler so OpenAI requests carrying `priority`
        # (lower = sooner) are honoured. A unit can explicitly opt back into FCFS.
        if "--scheduling-policy" not in spec["extra_args"]:
            cmd += ["--scheduling-policy", "priority"]
        if spec["max_model_len"]:
            cmd += ["--max-model-len", spec["max_model_len"]]
        cmd += spec["extra_args"]
        if "--enable-lora" not in spec["extra_args"]:
            cmd += lora_launch_args(spec)
        return cmd

    def env(self, spec: dict, base_env: Mapping[str, str]) -> dict:
        env = dict(base_env)
        # Only set this if the unit did not already. Setting it ONLY here is a
        # trap: the child then runs on the right card while THIS process — which
        # is where livestack's CUDA meter runs, and therefore where the device
        # pressure the planner reads comes from — still sees every card and
        # meters device 0. Observed: the LLM on card 1 reporting card 0's
        # pressure, identical to the three ASR/TTS nodes actually on card 0.
        # The unit sets CUDA_VISIBLE_DEVICES process-wide so the wrapper, its
        # meter and the child all agree.
        cuda = os.environ.get("HARMONY_LLM_CUDA_DEVICE", "1")
        if "CUDA_VISIBLE_DEVICES" not in env and cuda:
            env["CUDA_VISIBLE_DEVICES"] = cuda
        return env

    # -- is it up ----------------------------------------------------------
    def ready(self, spec: dict, timeout: float = 2.0) -> bool:
        try:
            r = httpx.get(f"http://127.0.0.1:{spec['port']}/health", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    # -- what did it cost ---------------------------------------------------
    def capture(self) -> LineCapture:
        from livestack_node.vllm_startup import StartupCapture
        return StartupCapture()

    def measure(self, spec: dict, capture, pid: "int | None",
                argv: "list[str]") -> Optional[dict]:
        """Turn the captured startup lines into the unit's measured cost.

        The lines print before the server answers, but the tee thread may still
        be a moment behind the readiness poll, so wait briefly for them. Whatever
        arrives, the unit gets an answer: a MeasuredCost, or `unknown` naming the
        lines that never came. Never 0, and never the declared prior silently.
        """
        from livestack_node.vllm_startup import composition_hash, key_from_launch
        deadline = time.time() + 10
        probe = capture.result()
        while getattr(probe, "unmatched", None) and time.time() < deadline:
            time.sleep(0.2)
            probe = capture.result()
        adapters = {n: r for n, (_, r) in adapters_for(spec).items()}
        # after `vllm serve <model>`
        serve_args = list(argv[3:])
        key = key_from_launch(spec["model"], serve_args, adapters,
                              engine_version=getattr(probe, "engine_version", ""))
        chash = composition_hash(key)
        row = capture.result(composition_hash=chash, now=time.time()).to_json()
        # The composition is the ENGINE's fact — which launch line this cost was
        # measured for — so it rides the row; the server only stores it.
        row["composition_hash"] = chash
        row["composition"] = json.loads(key.canonical())
        return row

    # -- what the launch line means -----------------------------------------
    def launch_attributes(self, spec: dict) -> dict:
        """A unit's attributes, with the launch-line facts DERIVED rather than
        trusted from the config file.

        `thinking` and `vision` are properties of how vLLM was started, and a
        hand-declared value that disagrees is the same silent failure in a new
        place: `"thinking": true` beside a unit with no reasoning parser routes a
        thinking request to a unit that will leak its narration into `content`.
        So the launch line wins, always.
        """
        attrs = dict(spec.get("attributes") or {})
        args = spec.get("extra_args")
        argv = args if isinstance(args, list) else shlex.split(str(args or ""))
        joined = " ".join(argv)
        # WHAT KIND OF WORK this unit serves is a launch-line fact, like every other
        # attribute here. vLLM started for pooling (`--task embed`, or `--runner
        # pooling` since v0.10) serves /v1/embeddings and answers /v1/chat/completions
        # with a 400; started for generation it does the exact opposite. So a
        # hand-declared `"class": "llm"` beside `--task embed` is the lying attribute
        # this docstring warns about — it would match a chat request's derived
        # `class=llm` and the unit would then refuse the very request it claimed.
        attrs["class"] = "embed" if is_pooling(joined) else "llm"
        # Separable reasoning requires a parser. Without one the model still
        # "thinks"; the narration just arrives inline in content.
        attrs["thinking"] = "--reasoning-parser" in joined
        # Tool calling is a launch-line fact too, and a harsher one: vLLM answers
        # `tool_choice: "auto"` with a 400 unless BOTH --enable-auto-tool-choice and
        # --tool-call-parser are set, so a unit lacking them cannot serve a
        # tool-calling request AT ALL, whatever its weights can do. Declaring
        # `"tools": true` beside such a unit is the lying attribute this docstring
        # warns about: the clause would match and the unit would then 400 the very
        # request it claimed to satisfy. Found 2026-09-07 by sending the Overlord's
        # own 46 tool schemas at the 27B and getting that 400 back.
        attrs["tools"] = ("--enable-auto-tool-choice" in joined
                          and "--tool-call-parser" in joined)
        # A vision-capable model started with --language-model-only is not a vision
        # unit for the purposes of routing, whatever its weights can do.
        if "--language-model-only" not in joined:
            attrs.setdefault("vision", spec.get("vision"))
        else:
            attrs["vision"] = False
        if attrs.get("vision") is None:
            attrs.pop("vision", None)
        # What this unit SERVES, not what the weights support. Declaring the
        # weights' 262144 next to a unit serving 16384 is an attribute that LIES:
        # a `context_len>=32768` requirement would match it and the request would
        # then be rejected by the very unit that satisfied the clause. An attribute
        # that lies is worse than one that is missing, because the missing one
        # fails the clause (silence is not a yes) and the lying one passes it.
        served = spec.get("max_model_len")
        if served:
            try:
                attrs["context_len"] = int(served)
            except (TypeError, ValueError):
                attrs.pop("context_len", None)
        # CONCURRENCY is a launch-line fact for the same reason: the queue below
        # a saturated engine is this number, and a declared one that disagrees
        # would let requests pile into an engine already at its scheduler limit.
        attrs["max_concurrent"] = max_concurrent(spec)
        # One attribute per adapter the launch line will actually load, so the
        # clause `adapter=<name>` selects a unit that serves it and nothing else.
        # Derived from the same resolution as the flags: it cannot claim an adapter
        # the engine was not started with.
        for key in [k for k in attrs if k.startswith("adapter.")]:
            attrs.pop(key)
        for name in adapters_for(spec):
            attrs[f"adapter.{name}"] = True
        return attrs

    def adapters(self, spec: dict) -> "dict[str, tuple[str, int]]":
        return adapters_for(spec)

    def adapter_launch_args(self, spec: dict) -> "list[str]":
        return lora_launch_args(spec)

    # -- how it dies --------------------------------------------------------
    def stop(self, proc) -> None:
        terminate_and_wait(proc, "vLLM")

    # -- refusal semantics ---------------------------------------------------
    def context_refusal(self, status: int, body: bytes) -> Optional[str]:
        # vLLM answers an over-long prompt with a 400 whose text names the numbers
        # exactly: "maximum context length is 16384 tokens ... your prompt contains
        # at least 16385 input tokens". Whether it is a context refusal is the
        # ENGINE's dialect; the routing consequence is the server's.
        if status != 400:
            return None
        text = body.decode("utf-8", "replace")
        return text if "context length" in text.lower() else None

    def _version_file(self, spec: dict) -> str:
        for name in ("VLLM_VERSION", "venv/VLLM_VERSION"):
            try:
                with open(os.path.join(SERVER_DIR, name), "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                continue
        return ""
