"""The engine seam of harmony-llm.

ONE question per engine: what do I launch (``argv``/``env``), is it up
(``ready``), what did it cost (``measure``), what does its launch line MEAN
(``launch_attributes``), how do I stop it (``stop``), and does this refusal
mean "the prompt is too long" (``context_refusal``). Everything else in this
server — routing, admission, the queue, forwarding — is engine-blind on
purpose: today's two engines are vLLM (a subprocess that owns a slice of a
card) and Strata (a llama.cpp-lineage C++/CUDA engine whose weights live in
host RAM and whose cache sizes itself to the free VRAM), and the day the
second one arrived with its own adapter is the day the seam paid for itself.

An engine is NEVER a requestable attribute: a caller states what it needs
(``class=llm,context_len=[131072,]``) and Harmony picks who serves it.
"""
from __future__ import annotations

import os
import signal
import subprocess
from typing import Callable, Mapping, Optional, Protocol


def terminate_and_wait(proc, label: str) -> None:
    """Stop an engine process and WAIT for it to die.

    Returning while the process is still exiting reports VRAM freed that the
    driver has not reclaimed yet, and the planner would then grant against
    memory that is still held. SIGTERM first (an engine flushes and exits
    cleanly), then SIGKILL for one that ignores it."""
    if proc is None or proc.poll() is not None:
        return
    print(f"[harmony-llm] stopping {label}", flush=True)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        print(f"[harmony-llm] {label} ignored SIGTERM; killing", flush=True)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()
        proc.wait(timeout=30)


class LineCapture:
    """Engine-agnostic log capture: every line in, engines dig what they need.

    A vLLM puts its memory report on stdout during startup; a Strata does not
    report at all (its estimate comes from the GGUF). The tee thread speaks
    only this interface, so a new engine brings its own digger and nothing
    else changes. Unbounded by design — the ENGINE bounds what it keeps
    (``vllm_startup.StartupCapture`` keeps a few lines); a raw-line buffer of a
    server's whole stdout would be the unbounded log this codebase keeps
    refusing to be.
    """

    def __init__(self) -> None:
        self.lines: "list[str]" = []

    def feed(self, line: str) -> None:
        self.lines.append(line)


class Engine:
    """What harmony-llm must know to run one unit's engine. Nothing more.

    The exact shape follows this server's needs; the CONTENT is fixed by
    `openspec/changes/harmony-offload-engine-units` design §2: the engine owns
    argv/env/ready-measure/stop and the attributes it can derive from its own
    launch line, and the server keeps routing, admission and the queue.
    """
    #: The name a units file selects it by ("vllm", "strata").
    name: str = ""

    def capture(self) -> LineCapture:
        """A fresh startup-line capture for one launch."""
        return LineCapture()

    def argv(self, spec: dict, budget: "Mapping[str, float] | None" = None,
             total_bytes: Optional[float] = None) -> "list[str]":
        """The full launch line, program first. ``budget`` is the planner's
        grant (``{"vram_bytes": ...}``); an engine that sizes itself takes it,
        one that does not ignores it. ``total_bytes`` (VRAM of the card, when
        the caller measured it) is offered so a test never needs a GPU."""
        raise NotImplementedError

    def env(self, spec: dict, base_env: Mapping[str, str]) -> dict:
        """The child's environment. The default passes the parent through
        untouched (the trap at server.py's CUDA_VISIBLE_DEVICES comment is
        about setting it ONLY for the child — engines that need a device get it
        from the unit spec's process-wide setting, not from here)."""
        return dict(base_env)

    def ready(self, spec: dict, timeout: float = 2.0) -> bool:
        """Is the engine actually answering on this unit's port?"""
        raise NotImplementedError

    def measure(self, spec: dict, capture, pid: "int | None",
                argv: "list[str]") -> Optional[dict]:
        """The unit's measured cost, in the unit-measured-cost shape
        (``{"measured": "strata-startup"|"vllm-startup"|"unknown",
        "footprint": ..., "min_footprint": ..., "unmatched": [...]}``), or None
        when this engine reports nothing (and the declared prior stands).

        May block briefly for its own log lines — the tee thread races the
        readiness poll. Never returns 0 and never silently returns the
        declared prior: "unknown" is an answer, silence is not."""
        raise NotImplementedError

    def launch_attributes(self, spec: dict) -> dict:
        """The unit's FULL attribute map (declared + what the launch line
        proves). An attribute that lies is worse than one that is missing, so
        launch-line facts always beat the config file."""
        raise NotImplementedError

    def stop(self, proc) -> None:
        """Stop the engine's process and WAIT for it to die. Returning while it
        is still exiting reports VRAM freed that the driver has not reclaimed."""
        raise NotImplementedError

    def adapters(self, spec: dict) -> "dict[str, tuple[str, int]]":
        """LoRA adapters this launch serves: name -> (path, rank). {} for
        engines without adapters."""
        return {}

    def adapter_launch_args(self, spec: dict) -> "list[str]":
        """The launch flags that make the adapters above served — [] for an
        engine that serves none (or serves them without flags)."""
        return []

    def context_refusal(self, status: int, body: bytes) -> Optional[str]:
        """Does this refusal mean "the prompt is too long"? Returns the
        vendor's text when it does (the router turns it into a routing fact);
        the default says no, so any other 400 passes through untouched."""
        return None

    def rev(self, spec: dict) -> str:
        """The engine revision this unit is PINNED to, for the ledger record:
        the operator's ``engine_rev``, else the engine's own version file, else
        "" (unknown — never a guess)."""
        declared = str(spec.get("engine_rev") or "").strip()
        return declared or self._version_file(spec)

    def _version_file(self, spec: dict) -> str:
        return ""


ENGINES: "dict[str, Engine]" = {}


def register(engine: Engine) -> None:
    ENGINES[engine.name] = engine


def engine_for(spec: Mapping) -> Engine:
    """The engine a unit spec selects. Unknown is a STARTUP ERROR naming the
    engine — a unit must not sit in the catalogue looking loadable and then
    fail on its first request (or, worse, fail silently and never serve)."""
    name = str(spec.get("engine") or "vllm")
    engine = ENGINES.get(name)
    if engine is None:
        raise ValueError(
            f"unit {spec.get('name')!r} names engine {name!r}, which is not "
            f"installed (known: {', '.join(sorted(ENGINES)) or 'none'})")
    return engine


# Import-and-register at the bottom: the modules above this line must not need
# the registry to exist yet.
from . import strata as _strata        # noqa: E402
from . import vllm as _vllm            # noqa: E402

register(_vllm.VllmEngine())
register(_strata.StrataEngine())
