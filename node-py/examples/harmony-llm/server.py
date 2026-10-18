"""A local LLM as a Harmony unit.

The point of routing an LLM through Harmony rather than letting it hold VRAM
statically: on a box that also serves ASR and TTS, the LLM is the largest
resident and the most interruptible. Harmony can shed it when someone is
dictating and bring it back when they stop. A bare `vllm serve` cannot be shed —
it owns the card until someone kills it.

Shape: vLLM runs as a SUBPROCESS, and the ManagedUnit's loader/freer start and
stop it. That is what makes eviction real — terminating the process is the only
way to return its VRAM to the driver, because the memory belongs to that process
(same reason the broker has a `reclaim` lever it cannot exercise itself).

This process holds no GPU memory of its own; it is a facade plus a proxy.
"""
from __future__ import annotations

import asyncio
import os
import json
import re
import shlex
import signal
import subprocess
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from livestack_node import request_log as _request_log
from livestack_node.decisions.simple_jev import SimpleJevError, classify as simple_jev_classify
from livestack_node.decisions.pane_ask_ingress import install_pane_ask_route
from livestack_node.decisions.resident_extraction_scope import ResidentExtractionScope
from livestack_node.decisions.pane_ask_runtime_install import register_pane_ask_runtime_installer
from livestack_node.demand_log import (LoadTimes, SpeedStats, UnitCostStore, UsageTail,
                                       demand_log_from_env, owner_namespace,
                                       requirement_hash)
from livestack_node.preferences import (PreferenceError as _PreferenceError,
                                        parse_preference_list as _parse_prefer,
                                        preference_key as _preference_key)
from livestack_node.vllm_startup import (REQUIRED as _STARTUP_REQUIRED, StartupCapture,
                                         composition_hash, key_from_launch)

# THE ENGINE SEAM (examples/harmony-llm/engines/): what it takes to drive one
# unit's engine — argv/env/ready/measure/stop and the launch-line attributes —
# lives behind `engine_for(spec)`. This module keeps routing, admission, the
# queue and forwarding, engine-blind. The package sits beside this file, so a
# script-style load (tests exec this module by path) must find it on the path.
import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from engines import engine_for as _engine_for          # noqa: E402
from engines import vllm as _vllm_engine               # noqa: E402
from selection import unit_inventory as _unit_inventory  # noqa: E402
from fleet_embed import (AmbiguousEmbedder, attrs_meet, embed_nodes as _embed_nodes,  # noqa: E402
                         fetch_fleets as _fetch_fleets, pick as _pick_embed)
from unit_queue import Queues as _Queues, QueueFull as _QueueFull   # noqa: E402
from fleet_route import (ROUTED_HEADER as _ROUTED_HEADER, SUPPORTED_CONTRACT as _SUPPORTED_CONTRACT,  # noqa: E402
                         RouteFailed as _RouteFailed, choose as _fleet_choose,
                         fleet_view as _fleet_view, mark_cold as _mark_cold,
                         new_request_id as _new_request_id, parse_routed as _parse_routed,
                         route_row as _route_row,
                         send_routed as _send_routed, view_age_s as _view_age_s,
                         wants_fleet_route as _wants_fleet_route)

HOST_ID = os.environ.get("HARMONY_LLM_HOST_ID", "xc-tower-ubuntu")
MODEL = os.environ.get("HARMONY_LLM_MODEL", "Qwen/Qwen3-8B")
NODE_PORT = int(os.environ.get("HARMONY_LLM_PORT", "8188"))
VLLM_PORT = int(os.environ.get("HARMONY_LLM_VLLM_PORT", "8189"))
GPU_FRACTION = os.environ.get("HARMONY_LLM_GPU_FRACTION", "0.60")
MAX_MODEL_LEN = os.environ.get("HARMONY_LLM_MAX_MODEL_LEN", "")
CUDA_DEVICE = os.environ.get("HARMONY_LLM_CUDA_DEVICE", "1")

# MORE THAN ONE MODEL, ONE PER UNIT.
#
# A node used to serve exactly one model, so "which model" and "which card" were
# the same question and both were answered in a systemd file. That is the
# decision Harmony exists to make: with two models declared as two units, the
# planner places them, and a workload that alternates between them settles them
# one per card because sharing would thrash (livestack planner `_contention_cost`).
#
#   HARMONY_LLM_UNITS='[{"name":"llm_title","model":"ibm-granite/granite-4.2-8b",
#                        "port":8189,"footprint_gb":18,"gpu_fraction":"0.85"},
#                       {"name":"llm_judge","model":"cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
#                        "port":8191,"footprint_gb":15,"gpu_fraction":"0.62"}]'
#
# Unset => exactly one unit named "llm" from the single-model variables above,
# so an existing deployment is byte-for-byte unchanged.
#
# Prefer HARMONY_LLM_UNITS_FILE for a systemd deployment: systemd processes
# quotes in `Environment=`, which silently mangles inline JSON — the value
# arrives with its property-name quotes stripped and json.loads fails at
# `line 1 column 3`. A file has no such rules.
_UNITS_FILE = os.environ.get("HARMONY_LLM_UNITS_FILE", "").strip()
_UNITS_ENV = os.environ.get("HARMONY_LLM_UNITS", "").strip()
if _UNITS_FILE:
    with open(_UNITS_FILE, "r", encoding="utf-8") as _fh:
        _UNITS_ENV = _fh.read().strip()
IDLE_EVICT_SECONDS = int(os.environ.get("HARMONY_LLM_IDLE_EVICT_SECONDS", "900"))
# Extra words for the `vllm serve` argv, split with shlex so quoted values
# survive ("--max-num-seqs 32 --dtype bfloat16"). Empty by default; model swaps
# (e.g. INT4 candidates needing --quantization) ride this instead of a code edit.
EXTRA_ARGS = shlex.split(os.environ.get("HARMONY_LLM_EXTRA_ARGS", ""))
# Warm-on-start: after attach, pre-load the LLM so the first request does not
# eat the cold start. Any value but "0"/"false" means on; set "0" to debug
# cold-start behaviour without the preload racing your observation.
WARM_ON_START = os.environ.get("HARMONY_LLM_WARM_ON_START", "1").strip().lower() not in {"0", "false"}
# Declared footprint. Qwen3-8B bf16 weights are ~16 GB; the rest is KV cache
# governed by GPU_FRACTION. The planner reconciles this against the live CUDA
# meter and uses the TIGHTER of the two, so an under-declaration here cannot
# cause an OOM grant — it would only make the planner over-optimistic until the
# meter corrects it.
FOOTPRINT = int(float(os.environ.get("HARMONY_LLM_FOOTPRINT_GB", "17")) * (1 << 30))
# Units in one contention class are alternatives for the same sort of work. The
# planner charges for co-residence in proportion to the DEMAND waiting for each,
# so alternating traffic separates them and idle siblings cost nothing.
SPREAD_GROUP = os.environ.get("HARMONY_LLM_SPREAD_GROUP", "llm")
# The kind this node advertises to livestack (see attach() below). Named here so
# _peer_at can ask "does that peer serve what I serve?" without re-spelling the
# literal, which is how the two would drift apart.
NODE_KIND = "llm"


# Added to every unit's vLLM port. THE reason two nodes can share one unit
# catalogue: a unit spec names a port, and two processes on one host cannot both
# bind it. With an offset per node, the SAME units file is declared by the node
# on card 0 and the node on card 1 — so every model is offered on every GPU and
# the planner actually has a choice of device.
#
# Without this, each node declares its own list, each unit exists on exactly one
# card in the planner's world, and "which model goes on which GPU" is decided in
# a service file again — the decision the planner exists to make.
PORT_OFFSET = int(os.environ.get("HARMONY_LLM_PORT_OFFSET", "0"))


def _is_pooling(joined: str) -> bool:
    """Was this unit started to POOL (embed) rather than GENERATE? (engine fact;
    see engines/vllm.py — kept as the module-level entry tests pin)"""
    return _vllm_engine.is_pooling(joined)


def _adapters_for(spec: dict) -> "dict[str, tuple[str, int]]":
    """The LoRA adapters a unit is started with: name -> (path, rank).
    Engine-owned (a Strata serves adapters differently, or not at all)."""
    return _engine_for(spec).adapters(spec)


def _lora_launch_args(spec: dict) -> "list[str]":
    """The engine's adapter launch flags, or [] for an engine without any."""
    return _engine_for(spec).adapter_launch_args(spec)


def _attributes_for(spec: dict) -> dict:
    """A unit's attributes: declared, with the launch-line facts the ENGINE
    derives always winning (an attribute that lies is worse than one that is
    missing — see engines/*.py)."""
    return _engine_for(spec).launch_attributes(spec)


def _unit_specs() -> "list[dict]":
    if not _UNITS_ENV:
        return [{"name": "llm", "model": MODEL, "port": VLLM_PORT,
                 "footprint_gb": FOOTPRINT / (1 << 30), "gpu_fraction": GPU_FRACTION,
                 "max_model_len": MAX_MODEL_LEN, "extra_args": EXTRA_ARGS,
                 "residency": None, "attributes": {},
                 "engine": "vllm", "engine_rev": "", "ram_gb": 0.0,
                 "exclusive_device": False}]
    out = []
    for spec in json.loads(_UNITS_ENV):
        out.append({
            "name": spec["name"],
            "model": spec["model"],
            "port": int(spec.get("port", VLLM_PORT)) + PORT_OFFSET,
            "footprint_gb": float(spec.get("footprint_gb", FOOTPRINT / (1 << 30))),
            # HOST RAM this unit pins — the weight a Strata maps stays in host
            # memory no matter what the card holds, and planning only its VRAM
            # is how a host swaps. Charged to the HOST pool, one pool behind
            # every card on the machine (planner `HOST_DIMS`).
            "ram_gb": float(spec.get("ram_gb", 0) or 0),
            # WHOLE-DEVICE claim: the engine sizes its cache to whatever VRAM
            # is free and refuses to start into a busy card. The planner charges
            # the device's entire capacity and admits it only when every other
            # tenant can leave. NOT requestable — how a unit runs is not what it
            # is.
            "exclusive_device": bool(spec.get("exclusive_device", False)),
            # WHICH ENGINE runs this unit ("vllm", "strata") and the rev it is
            # pinned to. The engine name is never an attribute a request can
            # require; both ride into the ledger record of what was loaded.
            # `engine_source` is the units file's PIN ({rev, sha256}): the
            # engine's `rev()` checks it against what is installed at startup.
            "engine": str(spec.get("engine") or "vllm"),
            "engine_rev": str(spec.get("engine_rev") or ""),
            "engine_source": dict(spec.get("engine_source") or {}),
            # Strata placement knobs (root checkout, `family/size` or a direct
            # .gguf path), read by engines/strata.py and ignored elsewhere.
            "strata_root": str(spec.get("strata_root") or ""),
            "strata_model": str(spec.get("strata_model") or ""),
            "gpu_fraction": str(spec.get("gpu_fraction", GPU_FRACTION)),
            "max_model_len": str(spec.get("max_model_len", MAX_MODEL_LEN) or ""),
            "extra_args": shlex.split(spec.get("extra_args", "")) or EXTRA_ARGS,
            "residency": spec.get("residency"),
            # Unit economics, declared by the operator: how long a fresh load
            # is protected (a 27B whose measured reload is ~50 s must not be
            # evicted 15 s in by a 0.6 B embedder), what a reload costs, and
            # this unit's claim on the card. All optional; absent keeps the
            # planner's defaults and the tier-derived priority, byte-for-byte.
            "min_residency_s": spec.get("min_residency_s"),
            "reload_cost": spec.get("reload_cost"),
            "priority": spec.get("priority"),
            # Operator intent, kept OUT of `attributes` on purpose: these say
            # which unit to pick and which to warm, not what a unit IS, and a
            # caller must never be able to require them.
            "default": bool(spec.get("default", False)),
            "warm_on_start": bool(spec.get("warm_on_start", False)),
            # What this unit IS, for requests that state a requirement rather
            # than a name. Carried verbatim to the broker; the planner compares,
            # it never interprets.
            "attributes": _attributes_for(spec),
            "adapters": dict(spec.get("adapters") or {}),
            # The unquantized model this one quantizes, as adapters name it; lets
            # the composer consider adapters on disk that no unit loads yet.
            "lora_base": spec.get("lora_base"),
        })
    if not out:
        raise RuntimeError("HARMONY_LLM_UNITS is set but declares no units")
    return out


SPECS = {u["name"]: u for u in _unit_specs()}


def _selection_rank(name: str) -> "tuple[int, str]":
    """Stable order among units that ALL satisfy the same requirement.

    Not declaration order. `next(n for n in SPECS ...)` made the answer to
    "which unit serves an indifferent request?" depend on which line of
    llm-units.json someone happened to type first: invisible in the config,
    unmentioned in the docs, and silently different after a reformat. With two
    interchangeable 27Bs declared, that decides which one every caller that
    stated no preference gets.

    An operator names the default explicitly with `"default": true`; everything
    else falls back to the unit NAME, which is stable across edits. This only
    ever breaks TIES — a requirement has already been applied before this runs,
    so ranking can never hand back a unit that does not satisfy the request.
    """
    return (0 if SPECS[name].get("default") else 1, name)


# -- unit selection under `prefer` (design §4b.3) ----------------------------
#
# `prefer` ORDERS units that already satisfied the hard requirement; it never
# swaps anything on its own (a resident unit keeps answering — scenario "a
# request both satisfy does not swap") and it never invents a value. The
# receipt says what matched, so a selection record can show WHY a unit won.

def _max_concurrent(name: str) -> int:
    """What this unit's ENGINE admits at once (launch-line fact, design §4a).
    The queue below a saturated engine is bounded by this and nothing else."""
    try:
        return max(1, int(_attributes_for(SPECS[name]).get("max_concurrent") or 1))
    except Exception:
        return 1


def _prefer_key(name: str, prefer):
    """(sort key, receipt) for one unit under `prefer`.

    `preference_key` already returns the receipt (a clause-by-clause account of
    what matched, what was comparable, what was silent) — the ordering key is
    just it, with the stable default/name order as the tiebreak."""
    if not prefer:
        return (_selection_rank(name), None)
    key, receipt = _preference_key(_inventory_for(name), prefer)
    return ((key, _selection_rank(name)), receipt)


def _inventory_for(name: str) -> dict:
    """One unit's `prefer` inventory. Speed measured on live requests wins over
    an offline-fitted cost row: it is newer and its sample count is real."""
    spec = SPECS[name]
    fitted = dict(_COSTS.load().get(_COMPOSITION.get(name, "")) or {})
    fitted.update(SPEED.summary(name))
    fitted.update(LOADS.summary(name))
    if spec.get("reload_cost") is not None:
        fitted.setdefault("reload_cost", spec["reload_cost"])
    revision = (f"measured:{_COMPOSITION[name][:19]}" if _COMPOSITION.get(name)
                else f"declared:{spec['model']}")
    return _unit_inventory(_attributes_for(spec), fitted, revision=revision)


def _ordered(prefer) -> "list[str]":
    return sorted(SPECS, key=lambda n: _prefer_key(n, prefer)[0])


def _expected_tokens(parsed_body: "dict | None") -> "int | None":
    """The answer length the caller allowed (`max_completion_tokens`, else
    `max_tokens`), or None. fleet_rank prices a cold unit by finish time, and
    a long answer is what repays a load on a faster host."""
    if not isinstance(parsed_body, dict):
        return None
    for key in ("max_completion_tokens", "max_tokens"):
        v = parsed_body.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def _prefer_from(parsed_body: "dict | None") -> "list[dict]":
    """The request's `harmony_prefer` clauses (design §4b: same vocabulary as
    fleet_rank's `prefer`, in the body beside `harmony_requires`)."""
    if not isinstance(parsed_body, dict):
        return []
    return _parse_prefer(parsed_body.get("harmony_prefer"))

# coload=False means acquiring ONE unit evicts the others IN THIS PROCESS. That
# is right for a node with a single model, and wrong the moment a node declares
# several for one card: the broker's SOFT_PIN restore of unit A and its
# demand-warm of unit B then fight, each load evicting the other, and neither
# finishes. Observed 2026-09-07 as a card that kept emptying itself.
#
# With several units, eviction belongs to the PLANNER — it knows the footprints,
# the demand and the whole card, and this process knows only its own units. So a
# multi-unit node coloads by default and lets Harmony decide what goes. An
# explicit false is a local safety invariant for a node whose units cannot fit
# together: even if the broker's residence snapshot briefly lags, the manager
# evicts this process's other unit before loading rather than trusting a stale
# grant and running vLLM into occupied VRAM.
_coload_env = os.environ.get("HARMONY_LLM_COLOAD")
COLOAD = (len(SPECS) > 1) if _coload_env is None else \
    _coload_env.strip().lower() in {"1", "true", "yes"}
try:
    from livestack_node.facade import resolve_device_id as _rdi
    DEVICE_ID_SELF = _rdi(HOST_ID)
except Exception:
    DEVICE_ID_SELF = ""
VLLM_BASE = f"http://127.0.0.1:{VLLM_PORT}"   # single-unit compatibility alias

# One vLLM subprocess per unit, keyed by unit name. Eviction is process death —
# that is the only thing that returns VRAM to the driver — so a unit's process
# is its residency.
_procs: "dict[str, subprocess.Popen]" = {}
_lock = threading.RLock()

# Set the moment this node is told to stop (SIGTERM/SIGINT, see `__main__`).
# `_load` blocks a request thread for up to HARMONY_LLM_START_TIMEOUT waiting on
# the engine, and uvicorn's graceful shutdown waits for that thread -- so a stop
# that arrives mid-load (a reboot a minute after boot) ran out systemd's whole
# stop timeout, because the engine ignores SIGTERM while it initialises.
_SHUTDOWN = threading.Event()

# Failed starts per unit: (consecutive failures, retry-not-before, last reason).
# A start that fails is retried on a doubling cooldown (30 s .. 10 min) instead
# of on every request; see `_load`.
_START_FAILURES: "dict[str, tuple]" = {}
_START_BACKOFF_S, _START_BACKOFF_MAX_S = 30.0, 600.0


def _note_start_failure(name: str, why: str) -> None:
    fails = _START_FAILURES.get(name, (0, 0.0, ""))[0] + 1
    wait = min(_START_BACKOFF_MAX_S, _START_BACKOFF_S * 2 ** (fails - 1))
    _START_FAILURES[name] = (fails, time.time() + wait, why)
    print(f"[harmony-llm] {name}: start failed ({why}), {fails} in a row; "
          f"next start not before {wait:.0f}s unless the broker grants one", flush=True)


def _broker_did_not_know(res: dict) -> bool:
    """Did a non-granting admit mean "I know no unit like that" (a transient
    gap: this node withholds its registration while a load is in flight), as
    opposed to "I know it and will not place it"? Read from the planner's own
    defer reason, carried in `defer_reason` or the plan summary. No reason at
    all reads as a refusal: when the two cannot be told apart, the broker's
    answer stands."""
    reason = res.get("defer_reason")
    if reason is None:
        m = re.search(r"defer \S+ \((.*)\)", str(res.get("plan") or ""))
        reason = m.group(1) if m else ""
    return reason.startswith("no unit satisfies")


# WHAT EACH ENGINE SAID IT COSTS, and which composition it said it for. Set
# after every successful start from the engine's own startup lines
# (livestack_node/vllm_startup.py); the declared `footprint_gb` is only a
# prior until then. Keyed by unit name.
_COMPOSITION: "dict[str, str]" = {}
_COSTS = UnitCostStore(os.environ.get("HARMONY_UNIT_COSTS_FILE") or os.path.join(
    os.path.expanduser("~"), ".cache", "livestack", "unit-costs.jsonl"))
# One record per forwarded request (openspec: inference-demand-log). Disabled,
# and saying so, when HARMONY_DEMAND_LOG_AGE_DAYS is unset.
DEMAND = demand_log_from_env(HOST_ID, log=lambda m: print(m, flush=True))
# What THIS node placed elsewhere (fleet_route.route_row), apart from what it
# served: the demand log's readers count a record as a request served.
ROUTES = demand_log_from_env(f"{HOST_ID}-routes", log=lambda m: print(m, flush=True))
# What each unit has MEASURED itself doing on this host (decode tok/s, first
# token), published per unit as `prefer` evidence (`_fleet_inventory`) so a
# caller asking for speed reaches the hardware that has delivered it. Re-read
# from the demand log at start: evidence a node earned survives its restart.
SPEED = SpeedStats()
# How long each unit took to load here: what a cold unit costs a caller before
# its first token, published as `llm.load_s` for fleet_rank's finish-time rule.
LOADS = LoadTimes(os.environ.get("HARMONY_UNIT_LOADS_FILE") or os.path.join(
    os.path.expanduser("~"), ".cache", "livestack", "unit-loads.jsonl"))
if getattr(DEMAND, "enabled", False):
    print(f"[harmony-llm] speed evidence: {SPEED.seed(DEMAND.path)} sample(s) "
          f"re-read from {DEMAND.path}", flush=True)


# Fields a measured-cost row gets from fitting, not from the engine's startup
# log; a re-measurement carries them forward (see `_record_measurement`).
_FITTED_KEYS = ("state_pages_per_seq", "prefill_tok_s", "decode_tok_s", "state_fit",
                "state_burst", "block_size_source")


def _tee_engine_output(proc: subprocess.Popen, capture: StartupCapture) -> None:
    """Pass the engine's output through to our own stdout (the journal still
    gets every line) while the capture keeps the few it needs. Runs until the
    engine exits: a pipe nobody drains would block vLLM on its next log line."""
    for line in iter(proc.stdout.readline, ""):
        print(line, end="", flush=True)
        capture.feed(line)


def _record_measurement(name: str, spec: dict, cmd: list, capture: StartupCapture) -> None:
    """Turn the captured startup lines into the unit's measured cost.

    The ENGINE digs its own report (`engines/*.py`, unit-measured-cost shape) —
    vLLM prints one, Strata does not and says `unknown` — and this side only
    keeps what it means for the unit: the reported cost, the composition it was
    measured for, and the persistence. Whatever arrives, the unit gets an
    answer: a MeasuredCost, or `unknown` naming what never came. Never 0, and
    never the declared prior silently."""
    # The engine process this node started: an engine that reads its own cost
    # off the process (Strata: NVML per pid, /proc for RAM) needs it. Passing
    # None here made every such reading `unknown` (zz-whitby 2026-10-18).
    proc = _procs.get(name)
    pid = proc.pid if proc is not None and proc.poll() is None else None
    row = dict(_engine_for(spec).measure(spec, capture, pid, cmd) or {})
    if not row:
        return                          # this engine reports nothing at all
    composition = row.pop("composition", None)
    chash = row.get("composition_hash") or ""
    if chash:
        _COMPOSITION[name] = chash
    unit = _UNITS.get(name)
    if unit is not None:
        # REPORTED, NOT YET THE ADMISSION NUMBER. `unit.footprint` stays the
        # declared prior. On xc-tower-ubuntu the measured minimum (weights +
        # activation + graphs + KV for one max-length request, ~24.6e9 B) plus
        # the broker's default 2 GB device reserve exceeds the card (25.3e9 B):
        # the reserve exists to cover activation that declared footprints omit,
        # and a measured footprint already contains it. Handing the planner the
        # measurement before that double count is fixed made a reload of this
        # unit unplaceable (2026-09-28). See HARMONY.md, "Unit composition".
        unit.measured_cost = row
    if row.get("measured") == "unknown":
        print(f"[harmony-llm] {name}: engine memory report did NOT parse "
              f"(missing {row.get('unmatched')}); footprint is UNKNOWN, not "
              f"{spec.get('footprint_gb')} GB", flush=True)
        return
    parts = ", ".join(
        f"{k} {row[k] / (1 << 30):.2f}" for k in
        ("weights_nontorch", "peak_activation", "kv_bytes", "cuda_graphs") if k in row)
    if "kv_tokens" in row:
        parts += f" = {row['kv_tokens']} tokens"
    print(f"[harmony-llm] {name}: measured {row['footprint'] / (1 << 30):.2f} GiB "
          f"({parts}) for {chash[:19] or 'an unhashed composition'}", flush=True)
    if not chash:
        # Cost rows are keyed by composition hash (base, adapters, KV dtype,
        # context — a vLLM launch). An engine that reports none (Strata) still
        # gives the planner its measurement through `unit.measured_cost`; there
        # is just no composition row to keep it under.
        return
    try:
        # KEEP WHAT WAS FITTED. A restart re-measures the startup numbers, but
        # state pages and service rates come from traffic and bursts
        # (`replay_validate --fit-state`, scripts/measure_kv_pages.py) and are
        # not in the startup log. Replacing the row wiped them on every
        # restart (found 2026-10-01: the composer silently fell back to the
        # blended rate).
        prior = _COSTS.load().get(chash) or {}
        kept = {k: prior[k] for k in _FITTED_KEYS if k in prior}
        _COSTS.put({**row, **kept, "unit": name, "host_id": HOST_ID,
                    **({"composition": composition} if composition else {})})
    except Exception as exc:              # persistence is for proposals, never for serving
        print(f"[harmony-llm] {name}: could not persist measured cost: {exc}", flush=True)


def _device_total_bytes() -> float:
    """Total VRAM of the card this node speaks for, or 0 when unknowable.
    (Engine-side fact; see engines/vllm.py.)"""
    return _vllm_engine.device_total_bytes()


def _base_of(name: str) -> str:
    return f"http://127.0.0.1:{SPECS[name]['port']}"


def _vllm_up(timeout: float = 2.0, name: str = "") -> bool:
    """Is this unit's engine actually answering on its port? Engine-owned
    (`ready`); the name stays because a dozen call sites and their tests say so."""
    name = name or next(iter(SPECS))
    return _engine_for(SPECS[name]).ready(SPECS[name], timeout=timeout)


def _foreign_listener(name: str) -> bool:
    """Is this unit's port answered by a vLLM THIS node did not start?

    `_vllm_up` asks the port, not the process, so on a host where two nodes read
    one units file without distinct HARMONY_LLM_PORT_OFFSETs, each node's
    "is my vLLM up?" is answered by the OTHER node's engine. Measured on
    xc-tower-ubuntu 2026-09-22: a bulk run of embedding requests reached the
    GPU-1 node, which does not hold `embed_multi`; the GPU-0 node did, on the
    same port 8210. The GPU-1 node ensured `embed_multi` locally -- with
    coload off that STOPPED the resident 27B -- and then reported its "own"
    embedder ready at once, because the port already answered. Titles and the
    typed-decision classifier were down 14:09-14:22, and the two nodes then
    took turns starting and stopping the 27B on its shared port.

    A listener we did not start is someone else's engine. Use it; never evict
    our own residents to "load" what is already being served.
    """
    p = _procs.get(name)
    ours = p is not None and p.poll() is None
    return not ours and _vllm_up(name=name)


def _load(name: str = "", device: "str | None" = None,
          budget: "dict | None" = None):
    """Start this unit's ENGINE and block until it actually serves.

    `device` is the placement the PLANNER chose, arriving through
    livestack's warm path. This node is pinned to one card for metering (a
    wrapper that saw every card would report card 0's pressure for a model on
    card 1 — the trap engines/vllm.py's env note records), so the assignment is
    checked against the card this node speaks for rather than used to move the
    process. A node that is told to load somewhere it does not serve says so,
    instead of quietly loading in the wrong place and letting the planner
    believe its own plan.

    Returning before the server is up would let Harmony mark the unit resident
    and let a request through to a port that is not listening yet.
    """
    name = name or next(iter(SPECS))
    spec = SPECS[name]
    engine = _engine_for(spec)
    with _lock:
        p = _procs.get(name)
        if p is not None and p.poll() is None and _vllm_up(name=name):
            return p
        # Starting here would bind-fail, yet the readiness poll below would see
        # the other engine answer and report OUR load a success.
        # DO NOT RESPAWN A START THAT JUST FAILED. Each attempt costs ~40 s of a
        # vLLM claiming the card and failing; on 2026-09-30 every request paid
        # it, 346 times. A load the broker explicitly granted (it names a
        # device) has had room made for it, so it skips the cooldown.
        fails, not_before, why = _START_FAILURES.get(name, (0, 0.0, ""))
        if device is None and time.time() < not_before:
            raise RuntimeError(
                f"{name}: start failed {fails}x (last: {why}); not retrying for "
                f"{not_before - time.time():.0f}s")
        if _foreign_listener(name):
            raise RuntimeError(
                f"{name}: port {spec['port']} is already served by a "
                f"{engine.name} this node did not start; refusing to load a second "
                f"copy. Give each node on this host its own HARMONY_LLM_PORT_OFFSET.")
        env = engine.env(spec, dict(os.environ))
        cmd = engine.argv(spec, budget)
        started = time.time()
        print(f"[harmony-llm] starting {engine.name} for {name}"
              f"{f' (planner chose {device})' if device else ''}: {' '.join(cmd)}", flush=True)
        proc = subprocess.Popen(cmd, env=env, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        _procs[name] = proc
        capture = engine.capture()
        threading.Thread(target=_tee_engine_output, args=(proc, capture),
                         name=f"{engine.name}-out-{name}", daemon=True).start()
        deadline = time.time() + float(os.environ.get("HARMONY_LLM_START_TIMEOUT", "900"))
        while time.time() < deadline:
            if _SHUTDOWN.is_set():
                # Not a failed start: no cooldown, and SIGKILL the engine now
                # (the unit is going away; a graceful stop of a half-loaded
                # engine only repeats the wait that SIGTERM already lost).
                _procs.pop(name, None)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                proc.wait(timeout=10)
                raise RuntimeError(f"{name}: node is shutting down; load abandoned")
            if proc.poll() is not None:
                # The return code alone is not a diagnosis. rc=2 here was
                # argparse rejecting `--disable-log-requests`, removed in vLLM
                # 0.28 — invisible until the journal was read by hand. Point at
                # the log that has the answer.
                _procs.pop(name, None)
                _note_start_failure(name, f"{engine.name} exited during startup "
                                          f"(rc={proc.returncode})")
                raise RuntimeError(
                    f"{engine.name} exited during startup of {name} "
                    f"(rc={proc.returncode}); see `journalctl -u harmony-llm` "
                    f"for its stderr")
            if _vllm_up(name=name):
                load_s = time.time() - started
                print(f"[harmony-llm] {engine.name} ready: {name} after {load_s:.1f}s",
                      flush=True)
                _START_FAILURES.pop(name, None)
                try:
                    LOADS.observe(name, load_s)
                except OSError as exc:    # evidence, never a reason to fail a load
                    print(f"[harmony-llm] {name}: could not record load time: {exc}",
                          flush=True)
                _record_measurement(name, spec, cmd, capture)
                return proc
            time.sleep(2)
        _free(name)
        _note_start_failure(name, "not ready before the deadline")
        raise RuntimeError(f"{engine.name} for {name} did not become ready "
                           f"before the deadline")


def _free(name: str = ""):
    """Stop this unit's engine and WAIT for it to die (engine `stop`). Returning
    while the process is still exiting would report VRAM freed that the driver
    has not reclaimed yet, and the planner would then grant against memory that
    is still held."""
    name = name or next(iter(SPECS))
    with _lock:
        p = _procs.pop(name, None)
        if p is None:
            return
        _engine_for(SPECS[name]).stop(p)
def _health_probe_for(name: str):
    """A per-unit functional probe bound to that unit's port.

    The probe must speak the unit's OWN endpoint: a pooling unit answers a chat
    completion with a 400, so probing one with chat would mark a perfectly
    healthy embedder permanently unhealthy — and, because this is the gate on
    admission, it would never serve a single request.
    """
    def probe(_model) -> bool:
        spec = SPECS[name]
        if _attributes_for(spec).get("class") == "embed":
            endpoint, payload = "/v1/embeddings", {"model": spec["model"], "input": "ok"}
        else:
            endpoint, payload = "/v1/chat/completions", {
                "model": spec["model"], "max_tokens": 1,
                "messages": [{"role": "user", "content": "ok"}]}
        try:
            r = httpx.post(f"{_base_of(name)}{endpoint}", json=payload, timeout=30)
            return r.status_code == 200
        except Exception:
            return False
    return probe


def _health_probe(_model) -> bool:
    """FUNCTIONAL probe, not a liveness ping: the unit is healthy only if it
    can actually complete. A vLLM that is up and answering /health while its
    engine has died would otherwise keep attracting traffic."""
    try:
        r = httpx.post(
            f"{VLLM_BASE}/v1/chat/completions",
            json={"model": MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ok"}]},
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


# ONE client for the loopback hop to vLLM, not one per request.
#
# `httpx.AsyncClient()` builds a connection pool AND an SSL context at
# construction — the latter reads the system CA bundle, which is why an
# exhausted descriptor table surfaces as `ssl.create_default_context()` raising
# EMFILE rather than as a socket error. Measured here 2026-09-22 03:48:29: a
# burst of 71 `/v1/classifier` requests in one second against the default 1024
# soft limit produced `OSError: [Errno 24] Too many open files` and five HTTP
# 500s. Simple Jev loops over questions, so ONE classifier call is several of
# these hops.
#
# A shared client pools the loopback connections instead of opening and
# discarding one set per request. It is never closed on a request path — the
# process outlives every request, and closing it would break every request
# after the first.
# WHO MAY SPEND THIS CARD.
#
# `/v1/classifier` had no credential check of any kind. Measured 2026-09-22:
# 1,697 calls in 24 h from ONE off-fleet host at a public address, every one
# `principal=-`, bursting to 71 requests per second — legitimate traffic (the
# benchday hub classifying pane attention), but the only thing standing between
# that endpoint and anyone else who found it was that nobody had.
#
# Same source and same semantics as `hostd`'s admission auth, deliberately: a
# second spelling of "who is asking" is a second thing to get wrong.
# `principals_from_env` returns None when NOTHING is configured, and None means
# AUTH IS OFF — today's behaviour, byte for byte — while an empty table means a
# source was configured and yielded nothing, which fails CLOSED. Both are said
# out loud at startup, because a security control that quietly disabled itself
# is the failure this guards.
#: Sentinel for "not read yet", distinct from None, which is a real answer
#: meaning NOTHING IS CONFIGURED and therefore auth is off.
_UNSET = object()
_CLASSIFIER_PRINCIPALS = _UNSET


def _classifier_principals():
    """The principal table, read once at startup. `None` = nothing configured
    = auth off; `{}` = a source was configured and yielded nothing, which fails
    closed. `fleet_auth.principals_from_env` owns that distinction and logs the
    cause; this only makes the read EAGER.

    Eager because the alternative was measured 2026-09-22 05:04-05:10: the table
    was installed unreadable by this service's user, the lazy read raised inside
    the request path, and a live caller took 73 HTTP 500s over six minutes. The
    same fault read at startup is one line and costs nothing.
    """
    global _CLASSIFIER_PRINCIPALS
    if _CLASSIFIER_PRINCIPALS is _UNSET:
        _load_classifier_principals()
    return _CLASSIFIER_PRINCIPALS


def _load_classifier_principals():
    """Read the credential source, and say which of the three states we are in."""
    global _CLASSIFIER_PRINCIPALS
    from livestack_node.fleet_auth import principals_from_env
    _CLASSIFIER_PRINCIPALS = table = principals_from_env(
        log=lambda m: print(m, flush=True))
    print(f"[classifier] auth is "
          + ("OFF — no credential source configured; any caller may spend this card"
             if table is None else
             f"ON — {len(table)} principal(s): "
             + ", ".join(sorted(p.name for p in table.values()))
             if table else
             "ON but the principal table is EMPTY — every caller is refused"),
          flush=True)

# RELOAD WITHOUT A RESTART.
#
# The table above is read at startup, so activating a new principal used to
# mean restarting the service -- which kills every in-flight classifier/title
# request and takes the vLLM units down for minutes. `kill -HUP` (or
# `systemctl reload harmony-llm`) re-reads the SAME source and swaps it in.
#
# The swap is one reference assignment and every request reads the module
# global once, so a request sees the old table or the new one, never a mix.
# A reload that cannot produce a clean table KEEPS THE OLD ONE and says why:
#   * source unset            -> the environment cannot change under a live
#                                process; nothing to reload
#   * empty table / unreadable / world-readable / malformed
#                             -> `principals_from_env` returns {} (fail closed
#                                is right at startup, wrong here: it would
#                                lock out every live caller on a typo)
#   * any entry rejected      -> a table that quietly lost a principal is the
#                                failure this must not cause
# The outcome is logged unconditionally. Only principal NAMES and the
# fingerprints fleet_auth already logs appear; never a token.
def _reload_classifier_principals() -> str:
    """Re-read the credential source. Returns the logged outcome line; the old
    table stays in force unless this returns a line starting `swapped`."""
    global _CLASSIFIER_PRINCIPALS
    try:
        from livestack_node.fleet_auth import principals_from_env
        notes: list = []
        table = principals_from_env(log=notes.append)
        for n in notes:
            print(n, flush=True)
        old = _CLASSIFIER_PRINCIPALS
        if table is None:
            msg = "KEPT old table: no credential source is configured in the environment"
        elif not table:
            msg = ("KEPT old table: the source yielded NO principals "
                   "(unreadable, missing, group/other-accessible, malformed or all entries rejected)")
        elif any("rejected" in n for n in notes):
            msg = "KEPT old table: one or more entries were rejected, refusing a partial table"
        else:
            _CLASSIFIER_PRINCIPALS = table  # the atomic swap
            was = set() if not old else {p.name for p in old.values()}
            now = {p.name for p in table.values()}
            msg = (f"swapped: {len(table)} principal(s) now active: {', '.join(sorted(now))}; "
                   f"added: {', '.join(sorted(now - was)) or '-'}; "
                   f"removed: {', '.join(sorted(was - now)) or '-'}")
    except Exception as e:  # a signal handler must never raise into the loop
        msg = f"KEPT old table: reload raised {type(e).__name__}: {e}"
    print(f"[classifier] principals reload: {msg}", flush=True)
    return msg


_SHARED_CLIENT = None  # type: ignore[var-annotated]
_SHARED_CLIENT_LOCK = asyncio.Lock()


async def _shared_client() -> httpx.AsyncClient:
    """The process-wide client for loopback calls to the vLLM unit."""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        async with _SHARED_CLIENT_LOCK:
            if _SHARED_CLIENT is None:
                _SHARED_CLIENT = httpx.AsyncClient(
                    timeout=float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")),
                    # Bounded on purpose. Unbounded pooling against a single
                    # upstream is the same descriptor problem one layer down.
                    limits=httpx.Limits(max_connections=int(
                        os.environ.get("HARMONY_LLM_MAX_CONNECTIONS", "64")),
                        max_keepalive_connections=16))
    return _SHARED_CLIENT


app = FastAPI(title="harmony-llm", version="1.0.0")
install_pane_ask_route(app, _classifier_principals)

# EAGER. The whole point: a credential source that cannot be read is a startup
# line, not a per-request 500 discovered by whoever was calling at the time.
_load_classifier_principals()


def _gpu_call(fn):
    with _lock:
        return fn()


from livestack_node import ManagedUnit, ResidencyPolicy, attach, counting  # noqa: E402
from livestack_node.client import admit  # noqa: E402

_UNITS = {
    name: ManagedUnit(
        name,
        # `device` is accepted, so livestack passes the planner's placement in
        # (ManagedUnit introspects the loader). A loader without it is called
        # as before, which is why every other node in the fleet is unaffected.
        loader=(lambda n=name: (lambda device=None, budget=None: _load(n, device, budget)))(),
        freer=(lambda n=name: (lambda: _free(n)))(),
        # A unit that pins host RAM declares `ram_gb` and gets a VECTOR
        # footprint: the card number and the host-RAM claim, planned by two
        # pools (planner HOST_DIMS). A unit without one stays the int it was.
        footprint=({**({"ram_bytes": spec["ram_gb"] * (1 << 30)}
                       if spec.get("ram_gb") else {}),
                    "vram_bytes": int(spec["footprint_gb"] * (1 << 30))}
                   if spec.get("ram_gb") else int(spec["footprint_gb"] * (1 << 30))),
        # WHOLE-DEVICE claim, and WHICH ENGINE runs it (rev pinned, for the
        # ledger record). Neither is requestable: how a unit runs is not what
        # it is (harmony-engine-units design §4, §6).
        exclusive_device=bool(spec.get("exclusive_device", False)),
        engine=str(spec.get("engine") or "vllm"),
        engine_rev=_engine_for(spec).rev(spec),
        # SOFT_PIN. Measured 2026-09-05: an evicted unit takes ~50.7 s to answer
        # its first request, against the hub's 35 s title timeout and 15 s
        # attention timeout — a cold start is a missed title every time. Not
        # HARD_PIN: HARD_PIN means "never preempted", and the point of routing
        # the LLM through Harmony is that it CAN shed the LLM under real
        # pressure — SOFT_PIN still evicts then, HARD_PIN refuses to.
        # PER-UNIT, falling back to the node default. One node now serves models
        # with different claims on the card: the hub's title model must stay warm
        # (a cold start costs a title), while an eval model used a few times a
        # day must not. With one policy for the whole node, the judge's SOFT_PIN
        # restore kept re-claiming a card that cannot hold both, evicting titles
        # each time round.
        residency_policy=getattr(
            ResidencyPolicy,
            str(spec.get("residency")
                or os.environ.get("HARMONY_LLM_RESIDENCY", "SOFT_PIN")).upper()),
        health_check=_health_probe_for(name),
        spread_group=SPREAD_GROUP,
        attributes=spec.get("attributes") or {},
        # Declared economics (see `_unit_specs`): carried to the coordinator's
        # /residence report and from there into the planner's Unit. None =
        # undeclared, and the node's report omits the field so the broker
        # keeps its defaults.
        min_residency_s=spec.get("min_residency_s"),
        reload_cost=spec.get("reload_cost"),
        priority=spec.get("priority"),
    )
    for name, spec in SPECS.items()
}
for _name, _u in _UNITS.items():
    _u.extra_report = (lambda n=_name: {"demand_log": DEMAND.status(),
                                        # Per-unit queue depth (design §4a):
                                        # what is in flight and what is waiting,
                                        # so a saturated unit is visible before
                                        # it is a pile-up.
                                        "queue": _QUEUES.status(n, _max_concurrent(n))})


def _readiness() -> dict:
    """What this node can serve ITSELF, not what answers on its ports.

    `_vllm_up` asks the PORT. On a host where two nodes read one units file
    without distinct `HARMONY_LLM_PORT_OFFSET`s, the other node's engine answers
    — so this node reported `ready: true` and `serving llm_title, embed_multi`
    for units it never started and does not hold.

    Measured on xc-tower-ubuntu 2026-09-23: `xc-tower-ubuntu-gpu0` ran no vLLM
    at all, yet advertised `kinds: ['llm']`, `ready: true` and both units. It
    sorts before `-gpu1`, so it won every `kind=llm` lookup and forwarded each
    request into gpu1's engine through this node's serialising proxy: 0.9 s at
    concurrency 1, p50 7.5 s at 25, and ~86% of the hub's classifier calls
    aborted on an 8 s deadline. `_foreign_listener` already told this module the
    difference; readiness simply did not ask it.

    A listener we did not start is someone else's engine. Saying so is the
    difference between a node that is cold — which the fleet handles, and which
    loads on demand — and a node that claims to be warm and is a proxy.
    """
    live = [n for n in SPECS if _vllm_up(name=n) and not _foreign_listener(n)]
    foreign = [n for n in SPECS if _foreign_listener(n)]
    if live:
        detail = "serving " + ", ".join(live)
    elif foreign:
        # NAMED, not silent. This is a misconfiguration an operator can fix in
        # one line, and the node is the only thing positioned to notice it.
        detail = ("no unit resident; " + ", ".join(foreign)
                  + " on this host are served by a vLLM this node did not start "
                    "— give each node its own HARMONY_LLM_PORT_OFFSET")
    else:
        detail = "no unit resident"
    return {
        "ready": bool(live),
        "detail": detail,
        "model": ", ".join(SPECS[n]["model"] for n in live) or MODEL,
    }


# This node PROXIES; it never takes a livestack lease per request, so the
# lease-derived in_flight the facade would otherwise infer reads 0 no matter how
# many generations are in flight. Count our own, and let the facade label it
# `in_flight_source: "server"` so a consumer can tell that 0 means idle.
_busy = counting()
# Per-unit admission queues (design §4a): at most `max_concurrent` in flight,
# bounded FIFO of 64 waiting, 429 with the queue state beyond that.
_QUEUES = _Queues()


def _ensure_while_counted(ensure):
    """Reserve the facade before a unit starts loading for this request.

    Once a newly loaded unit reports resident, queued admission can immediately
    evict it. Counting only when the upstream request is sent leaves a gap
    between `manager.ensure()` making the unit visible and the handler reaching
    `client.send()`. On success, ownership passes through send to the response
    body iterator.
    """
    _busy.acquire()
    try:
        return ensure()
    except BaseException:
        _busy.release()
        raise


def _release_slot(ctx: dict) -> None:
    """Give this request's queue place back (design §4a).

    Idempotent on purpose: the response paths release at three different points
    (stream end, upstream error, refusal replay) and an exception may beat any
    of them to it. A request that dies must not hold a queue place forever."""
    slot = ctx.pop("queue_slot", None)
    name = ctx.get("unit")
    if slot is not None:
        slot.release()
        try:
            if slot._queue.status()["in_flight"] == 0 and name:
                manager.units[name].busy = False
        except Exception:
            pass

def _hardware() -> dict:
    """What this node runs on, for an explanation — never a ranking input. The
    ranking reads measured speed; hardware says WHY one host measured faster
    (a 3D V-Cache CPU and DDR5 feed a host-RAM-resident MoE faster)."""
    out: dict = {}
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            out["cpu"] = next((ln.split(":", 1)[1].strip() for ln in fh
                               if ln.startswith("model name")), None)
        out["cpu_threads"] = os.cpu_count()
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            kb = next(int(ln.split()[1]) for ln in fh if ln.startswith("MemTotal:"))
        out["ram_gib"] = round(kb / (1 << 20), 1)
    except Exception:
        pass
    try:
        gpu = subprocess.run(
            ["nvidia-smi", f"--id={os.environ.get('CUDA_VISIBLE_DEVICES', '0').split(',')[0]}",
             "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip().split(", ")
        if len(gpu) == 3:
            out.update(gpu=gpu[0], gpu_mib=int(float(gpu[1])), driver=gpu[2])
    except Exception:
        pass
    return out


_HARDWARE = _hardware()


def _fleet_inventory() -> dict:
    """What the fleet ranks this node on: each unit's own `prefer` inventory
    (`fleet_rank._unit_evidence` reads the serving unit's, not a node average)
    and the hardware it was measured on."""
    return {"units": {n: _inventory_for(n) for n in SPECS}, "host": _HARDWARE}


manager, residence = attach(
    app, host_id=HOST_ID, kind=NODE_KIND, units=_UNITS,
    idle_seconds=IDLE_EVICT_SECONDS, coload=COLOAD,
    gpu_call=_gpu_call, port=NODE_PORT, readiness=_readiness,
    in_flight=_busy, inventory=_fleet_inventory,
)


@app.post("/harmony/route")
async def route_explain(request: Request):
    """DRY RUN: where would this chat request be served, and why — computed by
    the same functions the proxy uses, loading and forwarding nothing.

    Body: the chat-completions body you would send (`harmony_requires`,
    `harmony_prefer`, `model`, `tools`, ...). Answer: the requirement as
    resolved, this node's units in preference order with their receipts and
    measured speed, and — for a speed preference — the fleet ranking with each
    candidate's reason. `x-harmony-routed` on the request is honoured exactly as
    the proxy honours it."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="harmony: body must be JSON")
    requirement = _requirement_from(body)
    try:
        prefer = _prefer_from(body)
    except _PreferenceError as e:
        raise HTTPException(status_code=400, detail=f"harmony: {e}")
    derived = _derived_requirements("chat/completions", body)
    if requirement is not None and derived:
        requirement = {**requirement, **derived}
    local = []
    for n in _ordered(prefer):
        _k, receipt = _prefer_key(n, prefer)
        local.append({"unit": n,
                      "satisfies": requirement is None or _local_satisfies(n, requirement),
                      "resident": n in getattr(manager, "resident", ()),
                      "speed": SPEED.summary(n), "preferences": receipt})
    out = {"node": SELF_BASE, "host": HOST_ID, "hardware": _HARDWARE,
           "requirement": requirement, "prefer": prefer, "local": local}
    regions, regions_source = _request_regions(request, body)
    trigger = None
    if FLEET_URL:
        named = _model_choice(body)
        unit = named or _default_unit()
        trigger = _wants_fleet_route(
            prefer, request.headers,
            no_warm_local=not _local_ready(requirement, prefer, unit, regions))
    if trigger:
        view = _fleet_view(FLEET_URL)
        if view is None:
            out["fleet"] = {"decision": "no fleet view", "fleet": FLEET_URL}
        else:
            rec = _fleet_choose(view, requirement, prefer, SELF_BASE,
                                expected_tokens=_expected_tokens(body), regions=regions)[1]
            age = _view_age_s(FLEET_URL)
            rec.update(trigger=trigger, regions_source=regions_source, view_age_s=age,
                       view_stale=bool(age is not None and age > 10.0))
            out["fleet"] = rec
    else:
        out["fleet"] = {"decision": "not routed",
                        "why": ("no fleet broker configured" if not FLEET_URL else
                                "already routed" if request.headers.get(_ROUTED_HEADER) else
                                "a resident, unsaturated, in-policy unit here serves it, and "
                                "no speed preference asks for more")}
    return JSONResponse(out)
app.state.pane_ask_execution_scope = ResidentExtractionScope(
    manager=manager, queues=_QUEUES, busy=_busy, max_concurrent=_max_concurrent,
    engine_is_up=lambda name: _vllm_up(name=name), device_id=DEVICE_ID_SELF,
)
register_pane_ask_runtime_installer(app, host_id=HOST_ID, device_id=DEVICE_ID_SELF, units=manager.units)


# A unit whose vLLM died is NOT resident, whatever this process last believed.
#
# The subprocess can go without us: OOM-killed, killed by an operator, crashed
# after startup. The ManagedUnit still holds its model handle, so the node keeps
# reporting the unit resident and the PLANNER keeps reserving its footprint —
# budgeting for a model that does not exist. Observed 2026-09-07: a card that was
# physically empty was reported as 12.88 GB in use, and every placement onto it
# was refused for want of room that was actually free.
#
# So: reconcile against the processes we started, and tell the manager to drop
# what is gone. Cheap (a poll of Popen.poll()), and it runs regardless of whether
# anything is asking, because the wrong answer is what a planner reads.
def _reap_dead_units():
    while True:
        time.sleep(float(os.environ.get("HARMONY_LLM_REAP_SECONDS", "20")))
        try:
            for name in list(getattr(manager, "resident", ()) or ()):
                proc = _procs.get(name)
                if proc is not None and proc.poll() is None:
                    continue                      # alive
                if _vllm_up(name=name):
                    continue                      # someone else's, still serving
                print(f"[harmony-llm] {name} is marked resident but its vLLM is gone "
                      f"— dropping it so the planner stops reserving its footprint",
                      flush=True)
                _procs.pop(name, None)
                try:
                    # Keep lock order manager -> GPU, the same as `ensure`.
                    # Taking `_lock` first here while an ensure holds the
                    # manager guard and waits for `_lock` deadlocks both paths.
                    manager.request_evict(name)
                except Exception as e:            # never let the reaper die
                    # Print the TYPE too. This handler swallowed a NameError
                    # (`gpu_call` for `_gpu_call`) once every 20s for hours: the
                    # phantom residency was never dropped, the planner kept
                    # reserving 21 GB for a dead vLLM, and every request 503'd
                    # with the node reporting itself healthy. A reaper that
                    # cannot die must still say loudly what stopped it.
                    print(f"[harmony-llm] reap of {name} failed: "
                          f"{type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[harmony-llm] reaper error: {e}", flush=True)


threading.Thread(target=_reap_dead_units, daemon=True).start()


if WARM_ON_START:
    def _warm_on_start():
        # Give the facade a moment to bind before the first ensure, so the
        # load does not race attach's own startup bookkeeping.
        time.sleep(2)
        # Let the peer list populate before asking who holds what: every node
        # announces itself on startup, and a check against an empty list sees no
        # holder and loads.
        time.sleep(float(os.environ.get("HARMONY_LLM_WARM_SETTLE", "10")))
        # What is hot after a reboot is an OPERATOR decision and must not share
        # a mechanism with request routing. `next(iter(SPECS))` warmed whichever
        # unit was declared first, so reordering the config silently changed
        # what a cold node comes back holding. Units opt in with
        # `"warm_on_start": true`; absent any, the declared default; absent
        # both, nothing is warmed and the reason is printed rather than guessed.
        names = [n for n in sorted(SPECS, key=_selection_rank)
                 if SPECS[n].get("warm_on_start")]
        if not names:
            names = [n for n in sorted(SPECS, key=_selection_rank)
                     if SPECS[n].get("default")]
        if not names:
            print("[harmony-llm] warm-on-start: no unit declares warm_on_start "
                  "or default — warming nothing", flush=True)
            return
        if len(names) > 1:
            # This process owns ONE card. Warming two units that cannot coload
            # is the eviction fight described at COLOAD, so say so out loud
            # instead of letting them take turns unloading each other.
            print(f"[harmony-llm] warm-on-start: {len(names)} units flagged "
                  f"({', '.join(names)}) — they must fit this card together",
                  flush=True)
        # `warm_on_start` is a claim about the HOST, not about this process:
        # "this unit should be hot somewhere". Two nodes reading one units file
        # — the normal shape for a two-card box — each read it as "hot HERE" and
        # every flagged unit was loaded once per card. Measured on a 2x3090 box:
        # two 21.7 GB copies of the same 27B, both cards full, and no room left
        # for the small embedding unit the planner was then asked to place. It
        # then granted a device no LLM node speaks for, because the same card
        # carries one device id per tenant.
        #
        # A peer that already holds it IS the copy. This is the same question
        # the serving path asks before forwarding (`_peer_at`); warm-on-start
        # simply never asked it, and so decided placement locally — the one
        # thing this file says over and over that admission exists to take away
        # from a node.
        # `warm_on_start` is a claim about the HOST — "this unit should be hot,
        # once". A peer of our kind that already holds it IS that copy, so warm
        # nothing and let requests forward there (the serving path does this for
        # itself; see `_held_elsewhere` at the proxy).
        #
        # Deliberately NOT routed through `admit`: warming is an operator
        # decision about what a cold node comes back holding, and a planner that
        # refuses — as it does while its bookkeeping still reserves a card for a
        # vLLM that has since died — would leave the node holding nothing at all.
        # Placement arbitration belongs on the REQUEST path, where a refusal can
        # be reported to a caller instead of silently yielding an empty node.
        for n in names:
            held = _held_elsewhere(n)
            if held:
                print(f"[harmony-llm] warm-on-start: {n} already held by {held} "
                      f"— not loading a second copy", flush=True)
                continue
            if _foreign_listener(n):
                print(f"[harmony-llm] warm-on-start: {n}'s port {SPECS[n]['port']} is already "
                      f"served by a vLLM this node did not start — not loading. Two nodes "
                      f"share this port: give each its own HARMONY_LLM_PORT_OFFSET.", flush=True)
                continue
            try:
                manager.ensure(n)
                print(f"[harmony-llm] warm-on-start: {n} resident", flush=True)
            except Exception as e:
                print(f"[harmony-llm] warm-on-start failed for {n}: {e}", flush=True)

    threading.Thread(target=_warm_on_start, daemon=True).start()


BROKER_URLS = [u.strip() for u in
               os.environ.get("LIVESTACK_BROKER_URL", "").split(",") if u.strip()]
# The FLEET broker, for routing a speed preference across hosts (fleet_route).
# `LIVESTACK_BROKER_URL` lists the host broker first and the fleet broker after
# it, so the last of two entries is the fleet one; a node announcing to its
# host broker alone has no fleet to route over and keeps local behaviour.
FLEET_URL = (os.environ.get("HARMONY_LLM_FLEET_URL", "").strip()
             or (BROKER_URLS[-1] if len(BROKER_URLS) > 1 else ""))
# How the fleet names THIS node: the announced address (LIVESTACK_NODE_HOST).
SELF_BASE = (f"http://{os.environ.get('LIVESTACK_NODE_HOST', '127.0.0.1').strip()}"
             f":{NODE_PORT}")
# A single-unit node on a single-card box has nothing to arbitrate; going
# through admission there would only add a round trip. Set this when a node
# shares its card with other tenants that Harmony may need to shed for it.
MULTI_NODE = os.environ.get("HARMONY_LLM_ADMIT", "").strip().lower() in {"1", "true", "yes"}
# Long: admission BLOCKS while the broker evicts victims and warms the grant,
# and warming a 15 GB model is minutes, not seconds.
ADMIT_TIMEOUT = float(os.environ.get("HARMONY_LLM_ADMIT_TIMEOUT", "600"))
# How long a load refused for HOST MEMORY keeps asking while task attempts on
# this machine drain (host_ledger.py), and how often. The ceiling matches the
# proxy timeout's order: past it the caller gets the refusal and its reason.
DRAIN_WAIT_S = float(os.environ.get("HARMONY_LLM_DRAIN_WAIT_S", "900"))
DRAIN_POLL_S = float(os.environ.get("HARMONY_LLM_DRAIN_POLL_S", "5"))
# This engine's fleet credential. Sent as Authorization on every /admit; the
# broker resolves the owner above against this token's principal (delegating,
# prefix "" — a mesh-reachable engine relays what its hub asserted). Unset =
# no header, which is exactly right until the token rollout reaches this
# deployment: against a broker with no principals configured the admission
# path is unchanged.
_FLEET_TOKEN = os.environ.get("HARMONY_LLM_FLEET_TOKEN") or None


def _same_kind_peers() -> "list[tuple[str, str]]":
    """(host_id, base URL) of OTHER live nodes serving our kind, from /peers.

    Excludes us by HOST_ID, which is unique per node (one per card here), not by
    device_id — two nodes can share a device, which is the whole point.
    """
    out: "list[tuple[str, str]]" = []
    for base in BROKER_URLS:
        try:
            rows = httpx.get(f"{base}/peers", timeout=3.0).json()
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else rows.get("peers", [])
        for r in rows:
            if r.get("host_id") == HOST_ID:
                continue                      # ourselves
            # `suspect` is a missed heartbeat, not a death: a node busy serving
            # exactly the unit being asked about is the one most likely to be
            # late. Skipping it made a busy holder look like no holder, and the
            # asker loaded its own copy (the 2026-09-22 eviction). The /health
            # probe in _held_elsewhere is the liveness check; only `mia` is out.
            if r.get("state") not in (None, "fresh", "suspect"):
                continue                      # mia/stale: not somewhere to defer to
            kinds = r.get("kinds") or []
            if kinds and NODE_KIND not in kinds:
                continue
            url = (r.get("peer") or "")
            if url:
                out.append((str(r.get("host_id") or ""),
                            url.rsplit("/livestack", 1)[0]))
        break                                 # first broker that answered
    return out


def _held_elsewhere(unit: str) -> "str | None":
    """A peer of our kind that already HOLDS `unit` — resident or still loading.

    Warm-on-start has to ask this, or a host with one unit flagged
    `warm_on_start` and two nodes reading the same units file loads that unit
    ONCE PER NODE. Neither the planner nor residency alone can answer it:
    `/admit` grants each asker its own free card (a correct answer to "where may
    I put this?", and the wrong question), and `resident` stays false for the
    minutes a 27B takes to load — long enough for the peer to look, see nothing,
    and load its own copy. `loading` is what closes that window.
    """
    for _host, b in _same_kind_peers():
        try:
            h = httpx.get(f"{b}/health", timeout=3.0).json()
        except Exception:
            continue
        u = (h.get("units") or {}).get(unit)
        if isinstance(u, dict) and (u.get("resident") or u.get("loading")):
            return b
    return None


def _same_host(base: str) -> bool:
    """Is this peer on THIS machine (another card's node, another port)?"""
    from urllib.parse import urlparse
    host = (urlparse(base).hostname or "").lower()
    mine = os.environ.get("LIVESTACK_NODE_HOST", "127.0.0.1").strip().lower()
    return host in {mine, "127.0.0.1", "localhost", "::1"}


def _held_on_host(unit: str) -> "str | None":
    """The request path's peer shortcut: a same-HOST peer already holding `unit`
    RESIDENT. Narrower than `_held_elsewhere` on purpose -- that one is
    warm-on-start's (any peer, resident or still loading, to avoid loading a
    second copy); on a request it made a sequential 3 s probe per remote peer
    and forwarded across the mesh on a unit that was only loading. A remote
    holder is the fleet ranking's business (fleet_route), not this shortcut's."""
    for _host, b in _same_kind_peers():
        if not _same_host(b):
            continue
        try:
            h = httpx.get(f"{b}/health", timeout=3.0).json()
        except Exception:
            continue
        u = (h.get("units") or {}).get(unit)
        if isinstance(u, dict) and u.get("resident") and not u.get("loading"):
            return b
    return None


def _peer_at(device_id: str) -> "str | None":
    """The base URL of the node that speaks for this device, when it is not us.

    From the broker's `/peers`, which is the only surface that carries a node's
    URL: `/status` reports device_id, memory and units but no address, so an
    earlier version of this that read `node_url`/`base` off `/status` could
    never match anything and silently never forwarded.

    Without forwarding, a node asked for a unit the planner placed elsewhere
    would load a second copy on its own card — deciding placement again, which
    is the thing admission exists to take away from it.

    A DEVICE IS NOT A NODE. Several livestack nodes share one card — polytts,
    polyasr and this one all register against the same device — so matching on
    device_id alone picks whichever co-tenant the broker happens to list first
    and forwards an LLM request to it. Observed exactly that way: an embeddings
    call granted `xc-tower-ubuntu/gpu0` was forwarded to the polytts node, which
    has no /v1/embeddings and answered 404 — a reply that looks like the model
    is missing rather than like this node sent it to a TTS server. So a peer
    must also serve OUR kind; one that serves something else is a neighbour on
    the card, not a stand-in for us.
    """
    if not device_id:
        return None
    for base in BROKER_URLS:
        try:
            rows = httpx.get(f"{base}/peers", timeout=3.0).json()
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else rows.get("peers", [])
        # PREFER A POSITIVE MATCH. "Did not say" stayed eligible so an older
        # node that reports no kinds could still be forwarded to — but the
        # broker also lists ALIAS rows (the same server reached by a second
        # address), and those carry no kinds. So the polytts node slipped
        # through its own alias row and answered an embeddings call with a 404.
        # A row that names our kind is always the better answer; an undeclared
        # one is a last resort, not a peer.
        best = None
        for r in rows:
            if r.get("device_id") != device_id:
                continue
            url = (r.get("peer") or "")
            if not url or r.get("device_id") == DEVICE_ID_SELF:
                continue
            kinds = r.get("kinds") or []
            if kinds and NODE_KIND not in kinds:
                continue
            base = url.rsplit("/livestack", 1)[0]
            if kinds:
                return base                   # says it serves our kind
            if best is None and not r.get("alias_of"):
                best = base                   # undeclared, and not a duplicate row
        return best
    return None


# SPECIALIST-ONLY ATTRIBUTES, mirroring the planner's rule (planner.py
# `_specialist_only`): a unit declaring one (e.g. `ocr: true`) serves work
# generic demand can never imply, so a requirement that merely matches
# `class=llm` must never land on it — not via the broker's placement, and not
# via the local resident-reuse path below. Measured 2026-09-19: a generic chat
# request was answered by the OCR unit, which had stopped the resident 27B to
# load. Stating the attribute in the requirement, or naming the unit, still
# works — only generic selection is refused.
_LOCAL_SPECIALIST_ONLY = ("ocr",)


def _local_satisfies(name: str, requires: dict) -> bool:
    """Does one of THIS node's units meet the requirement?

    Same semantics as the planner's `_unit_satisfies`, including the important
    one: an attribute the unit does not declare is NOT satisfied. Silence is
    not a yes, or an unlabelled unit would answer every question.
    """
    attrs = _attributes_for(SPECS[name]) if name in SPECS else {}
    # A specialist-only attribute must be NAMED to be served by. `_attributes_for`
    # derives class/thinking/tools/vision from the launch line but never `ocr`,
    # so nothing generic can imply it — a unit declaring it that matched on
    # `class=llm` alone would be the Sep 19 poach all over again.
    for attr in _LOCAL_SPECIALIST_ONLY:
        if attr in requires:
            continue
        val = attrs.get(attr)
        if val is True or (isinstance(val, str)
                           and val.strip().lower() in ("true", "1", "yes")):
            return False
    return attrs_meet(attrs, requires)


from request_language import (derived_requirements as _derived_requirements,  # noqa: E402
                              expand_clause as _expand_clause, num as _num,
                              requirement_from as _rl_requirement_from,
                              split_clauses as _split_clauses)


def _requirement_from(body_json: dict) -> "dict | None":
    """The request's stated NEED (request_language.requirement_from), with this
    node's adapter names read back as the requirement they stand for."""
    return _rl_requirement_from(
        body_json, lambda model: any(model in (SPECS[n].get("adapters") or {}) for n in SPECS))


def _named_unit(requested: str) -> "str | None":
    """The unit this `model` field NAMES, or None if it names nothing here.

    Separate from `_unit_for_model` because the difference matters: naming a
    unit is a caller's decision and must be honoured, while naming nothing is
    a caller with no opinion and may be resolved however the node likes.
    """
    r = (requested or "").strip()
    if r in SPECS:
        return r
    for name, spec in SPECS.items():
        if spec["model"] == r:
            return name
    return None


def _model_choice(body) -> "str | None":
    """The unit the `model` field CHOOSES, or None when the caller has no
    opinion.

    A unit name, a model id — and the legacy alias `local`, which every
    existing caller sends meaning this node's DEFAULT model. The alias is a
    CHOICE, not "no opinion": a resident flash_next would otherwise answer for
    the 27B through the reuse shortcut with nothing in the exchange saying so
    (harmony-engine-units scenario: "Named local still reaches the 27B ...
    never by flash_next"). `_named_unit` stays strict — "does this name a
    UNIT?" — because that answer is needed too ("no opinion must be TELLABLE
    from asked for something"); the alias resolves through `_unit_for_model`,
    which is where its old meaning has always lived."""
    r = str((body or {}).get("model") or "").strip()
    named = _named_unit(r)
    if named is not None:
        return named
    return _unit_for_model(r) if r == "local" else None


def _default_unit() -> "str | None":
    """The unit `default: true` names, else the first declared — what an
    indifferent caller gets, deterministically (never "whichever unit happens
    to be first in the dict this boot")."""
    named = next((n for n in sorted(SPECS, key=_selection_rank)
                  if SPECS[n].get("default")), None)
    return named or next(iter(SPECS), None)


def _unit_for_model(requested: str) -> str:
    """Which declared unit serves this `model` field.

    Accepts the unit name, the model id, or the legacy alias `local`. An
    unknown model resolves to the DEFAULT unit (the `default: true` one, else
    the first declared) rather than erroring, which keeps every existing
    caller — all of which send `local` — working.
    """
    return _named_unit(requested) or _default_unit()


@app.get("/health")
def health():
    # `loading` is reported alongside `resident` because the two are different
    # answers to "is this unit yours?" and only one of them was visible. A peer
    # deciding whether to warm a unit saw `resident: false` for a unit whose
    # vLLM had been starting for four minutes, concluded nobody had it, and
    # loaded a second copy of a 27B onto the other card. A process that exists
    # but is not yet serving is a CLAIM on that unit, and a claim nobody can see
    # is the same as no claim at all.
    return {"status": "ok",
            "units": {n: {"model": SPECS[n]["model"],
                          "resident": _vllm_up(name=n),
                          "loading": n in _procs and not _vllm_up(name=n)}
                      for n in SPECS},
            # Single-unit shape, kept so existing health checks still parse.
            "model": SPECS[next(iter(SPECS))]["model"],
            "resident": any(_vllm_up(name=n) for n in SPECS),
            # What a router rollout pins to: the revision this node runs and the
            # routed-request contract it speaks (fleet_route.SUPPORTED_CONTRACT).
            "revision": _REVISION, "contract": _SUPPORTED_CONTRACT,
            "region": NODE_REGION or None}


def _card_total_bytes() -> "int | None":
    """The card's total memory from nvidia-smi, or None when unknowable.

    NOT `_device_total_bytes()`: that asks torch, and a first torch.cuda call
    creates a CUDA context in THIS process — hundreds of MB taken from a card
    whose engine leaves ~1 GiB free, to answer a read-only question."""
    dev = (os.environ.get("CUDA_VISIBLE_DEVICES") or CUDA_DEVICE or "0").split(",")[0]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits",
                              "-i", dev], capture_output=True, text=True, timeout=5)
        return int(float(out.stdout.strip().splitlines()[0]) * (1 << 20)) if out.returncode == 0 else None
    except Exception:
        return None


@app.get("/composition/facts")
def composition_facts(since: float = 0.0, limit: int = 50_000):
    """Everything a composition decision reads from this node: each unit's live
    composition (as launched, not as declared elsewhere), every measured cost
    this host has recorded, the engine facts, and the demand trace since
    `since`. Read-only; the composer (livestack_node/compose.py) decides and
    nothing here applies anything."""
    from livestack_node.demand_log import read_demand
    from livestack_node.vllm_startup import _flag
    units = []
    for name, spec in SPECS.items():
        args = list(spec.get("extra_args") or [])
        units.append({
            "name": name, "model": spec["model"],
            "adapters": {n: r for n, (_, r) in _adapters_for(spec).items()},
            "adapter_paths": dict(spec.get("adapters") or {}),
            # Which unquantized model this served one is a quantization of, as
            # the adapters on disk name it (`base_model_name_or_path`). Declared,
            # because nothing in a quantized checkpoint says so reliably; a unit
            # without it can only be composed with the adapters it already has.
            "lora_base": spec.get("lora_base"),
            "kv_dtype": _flag(args, "--kv-cache-dtype") or "auto",
            "max_model_len": int(spec.get("max_model_len") or 0),
            "max_num_seqs": int(_flag(args, "--max-num-seqs") or 0),
            "gpu_fraction": float(spec.get("gpu_fraction") or 0),
            "extra_args": " ".join(args),
            "residency": str(spec.get("residency") or os.environ.get("HARMONY_LLM_RESIDENCY", "SOFT_PIN")).upper(),
            "resident": name in getattr(manager, "resident", ()),
            "composition_hash": _COMPOSITION.get(name),
            "measured": getattr(_UNITS.get(name), "measured_cost", None),
        })
    catalogue = []
    root = os.environ.get("HARMONY_ADAPTER_DIR", "/var/lib/harmony/adapters")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(root, n, "adapter_config.json"), "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            catalogue.append({"name": n, "path": os.path.join(root, n), "rank": int(cfg["r"]),
                              "lora_base": cfg.get("base_model_name_or_path")})
        except Exception as exc:          # named, not skipped: an unreadable adapter is a finding
            catalogue.append({"name": n, "path": os.path.join(root, n), "error": f"{type(exc).__name__}: {exc}"})
    trace = read_demand(DEMAND.path, since, limit) if DEMAND.enabled else []
    return {
        "host_id": HOST_ID, "device_id": DEVICE_ID_SELF,
        "capacity_bytes": _card_total_bytes(),
        # Engine facts as data: which KV dtypes this host's engine can start
        # with. fp8 needs the gcc-14 NVCC drop-in on xc-tower-ubuntu, so it is
        # listed only where an operator has said so.
        "kv_dtypes": [d.strip() for d in os.environ.get("HARMONY_KV_DTYPES", "auto").split(",") if d.strip()],
        "units": units,
        "adapter_catalogue": catalogue,
        "measured_rows": list(_COSTS.load().values()),
        "demand_log": DEMAND.status(),
        "trace": trace, "trace_truncated": len(trace) >= limit,
        "now": time.time(),
    }


@app.post("/v1/classifier")
async def classifier(request: Request):
    """Simple Jev v1 scoring through this node's normal resident-model route."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="classifier body must be JSON") from exc

    owner = request.headers.get("x-harmony-owner")
    authorization = request.headers.get("authorization")

    # UNCONDITIONAL, with the condition in the message. A line that appeared
    # only on refusal could not answer the question this endpoint actually
    # raised — "does the caller present a credential at all?" — until the day
    # somebody turned enforcement on and found out by breaking it.
    principals = _classifier_principals()
    label = _request_log.principal_label(authorization, principals)
    print(f"[classifier] auth={'REQUIRED' if principals is not None else 'off'} "
          f"credential={label or 'none presented'}", flush=True)
    if principals is not None:
        from livestack_node.fleet_auth import AuthError, authenticate
        try:
            # The owner header is the delegated account, checked against the
            # principal's prefix exactly as `/fleet/admit` checks it.
            authenticate(principals, authorization, owner)
        except AuthError as e:
            raise HTTPException(status_code=e.status, detail=e.detail)

    async def invoke_chat(body: dict) -> dict:
        headers = {"content-type": "application/json", "x-harmony-origin": "classifier"}
        if owner:
            headers["x-harmony-owner"] = owner
        if authorization:
            headers["authorization"] = authorization
        client = await _shared_client()
        response = await client.post(
            f"http://127.0.0.1:{NODE_PORT}/v1/chat/completions", json=body, headers=headers)
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text[:1000])
        return response.json()

    try:
        return JSONResponse(await simple_jev_classify(payload, invoke_chat))
    except SimpleJevError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_DEMAND_PATHS = {"chat/completions", "completions", "embeddings"}


def _note_demand(ctx: dict, outcome: str, status: int, tail: "UsageTail | None" = None) -> None:
    """One demand record for a request this node served or refused itself.
    A request forwarded to a peer is that peer's record, not ours."""
    if ctx.get("path") not in _DEMAND_PATHS:
        return
    if ctx.get("forwarded"):
        if ctx.get("fleet_route", {}).get("chosen"):
            ROUTES.record(**_route_row(ctx, HOST_ID, outcome, status))
        return
    prompt, completion = tail.tokens() if tail is not None else (None, None)
    unit = ctx.get("unit")
    speed = (tail.speed(ctx.get("sent_at")) if tail is not None
             else {"decode_tok_s": None, "first_token_ms": None, "speed_source": None})
    if outcome == "ok" and unit:
        SPEED.observe(unit, speed["decode_tok_s"], speed["first_token_ms"],
                      in_flight=ctx.get("in_flight") or 1)
    DEMAND.record(
        ts=ctx["t0"], unit=unit, composition_hash=_COMPOSITION.get(unit or ""),
        adapter=ctx.get("adapter"), route=ctx.get("route") or ctx.get("path"),
        owner_ns=ctx.get("owner_ns"), principal=ctx.get("principal"),
        requirement_hash=ctx.get("requirement_hash"),
        prompt_tokens=prompt, completion_tokens=completion, n=ctx.get("n"),
        elapsed_ms=round((time.time() - ctx["t0"]) * 1000, 1),
        # How long this request waited in the unit's queue (design §4a): a
        # demand record that cannot say that cannot show a saturated unit.
        queue_ms=(round(ctx["queue_ms"], 1) if ctx.get("queue_ms") is not None else None),
        # WHY this unit won, when the caller stated a preference (§4b.3): the
        # preference_key receipt, clause by clause.
        preference_receipt=ctx.get("preference_receipt"),
        shortcut=ctx.get("shortcut"),
        # Measured speed and the unit's concurrency when it ran (SpeedStats
        # keeps solo and burst apart; `seed` re-reads these at start).
        decode_tok_s=speed["decode_tok_s"], first_token_ms=speed["first_token_ms"],
        speed_source=speed["speed_source"], in_flight=ctx.get("in_flight"),
        fleet_route=ctx.get("fleet_route"),
        # The id that joins the router's route record to this node's demand
        # record, who routed it here, and why a warm-routed request was refused.
        request_id=ctx.get("request_id"), routed_by=ctx.get("routed_by"),
        route_stale=ctx.get("route_stale"),
        # How long the load waited for task attempts to drain (host_ledger.py).
        drain_ms=ctx.get("drain_ms"),
        outcome=outcome, http_status=status)


def _outcome_for(status: int, detail: str = "") -> str:
    if status == 503 and "nothing satisfies" in detail:
        return "unsatisfied"
    if status in (502, 504):
        return "transport"
    return "refused"


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    """Serve the request and leave one demand record for it, whatever happens."""
    routed = _parse_routed(request.headers.get(_ROUTED_HEADER))
    ctx = {"t0": time.time(), "path": path,
           # Minted by the first node that routes it, then carried unchanged so a
           # router's route record and the target's demand record join on it.
           "request_id": (request.headers.get("x-request-id") or "").strip() or _new_request_id(),
           "routed_in": routed, "routed_by": routed.router_id if routed else None,
           "route": request.headers.get("x-harmony-origin"),
           "owner_ns": owner_namespace(request.headers.get("x-harmony-owner"))}
    try:
        ctx["principal"] = _request_log.principal_label(
            request.headers.get("authorization"), _classifier_principals())
    except Exception:
        ctx["principal"] = None
    if routed is not None and not 1 <= routed.version <= _SUPPORTED_CONTRACT:
        # A router speaking a contract this build does not: refuse, naming both,
        # rather than serving a request whose warm/cold promise we cannot read.
        _note_demand(ctx, "refused", 409)
        return JSONResponse(status_code=409, content={
            "error": "route_contract", "router_version": routed.version,
            "supported": _SUPPORTED_CONTRACT})
    try:
        return await _proxy_impl(path, request, ctx)
    except HTTPException as e:
        _release_slot(ctx)      # idempotent; the stream paths release too
        _note_demand(ctx, _outcome_for(e.status_code, str(e.detail)), e.status_code)
        raise
    except BaseException:
        _release_slot(ctx)      # a request that dies must not hold a queue place
        raise


class _Retargeted:
    """A request re-shaped for its ONE context re-route: same verb, headers and
    query, a body with the need stated as a requirement. Only the body differs;
    everything the caller sent rides along unchanged — and so does the response
    it gets back (bytes, stream or not)."""

    def __init__(self, request: Request, body: bytes):
        self._request = request
        self._body = body
        self.method = request.method
        self.headers = request.headers
        self.query_params = request.query_params

    async def body(self) -> bytes:
        return self._body


async def _forward_to_embed_node(node: dict, parsed_body: dict, request: Request,
                                 ctx: dict, requirement: dict):
    """Send an embeddings request to the fleet embed node chosen for it.

    The body is restated in the node's own terms (its model id; ours are
    `require:` strings and `harmony_requires`, which it would not know). A dead
    node is a 502 that NAMES it -- an embedding request that vanishes into an
    empty reply is how a search quietly stops being semantic."""
    ctx["forwarded"] = True           # the node's demand record, not ours
    out = {k: v for k, v in parsed_body.items() if k != "harmony_requires"}
    out["model"] = node["model"]
    print(f"[harmony-llm] {requirement} -> {node['model']} @ {node['base']} (fleet embed node)",
          flush=True)
    headers = {"content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(float(os.environ.get(
                "HARMONY_LLM_PROXY_TIMEOUT", "300")))) as client:
            resp = await client.post(f"{node['base']}/v1/embeddings", json=out, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"embed node {node['base']} failed: {type(e).__name__}: {e or '(no message)'}")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


def _request_principal(request: Request):
    """The `fleet_auth.Principal` the caller's bearer token names, or None
    (auth off, no credential, or an unknown one -- refusing is not this
    function's job; it only resolves an allowance)."""
    try:
        from livestack_node.fleet_auth import bearer_token, principal_for
        table = _classifier_principals()
        token = bearer_token(request.headers.get("authorization"))
        return principal_for(table, token) if table and token else None
    except Exception:
        return None


def _request_regions(request: Request, parsed_body) -> "tuple[list[str] | None, str]":
    """Regions this request may run in: the principal's allowance, narrowed by
    `harmony_regions`, never widened (a 403 naming region and allowance)."""
    from livestack_node.fleet_auth import AuthError, effective_regions
    asked = parsed_body.get("harmony_regions") if isinstance(parsed_body, dict) else None
    try:
        regions, source = effective_regions(_request_principal(request), asked)
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail=f"harmony: {e.detail}")
    return regions, source


NODE_REGION = os.environ.get("LIVESTACK_NODE_REGION", "").strip().lower()


def _git_revision() -> str:
    """The livestack revision this process runs (HARMONY_LLM_REV overrides)."""
    env = os.environ.get("HARMONY_LLM_REV", "").strip()
    if env:
        return env
    try:
        return subprocess.run(["git", "-C", _HERE, "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


_REVISION = _git_revision()


def _local_ready(requirement, prefer, unit, regions) -> bool:
    """Can THIS node serve the request right now with no load and no queue: a
    resident, running, unsaturated unit that meets the requirement, in a region
    the request allows. False sends the request to be ranked over the fleet."""
    if regions and NODE_REGION not in regions:
        return False
    cands = ([n for n in _ordered(prefer) if _local_satisfies(n, requirement)]
             if requirement is not None else [unit])
    return any(n in getattr(manager, "resident", ()) and _vllm_up(name=n)
               and _QUEUES.has_capacity(n, _max_concurrent(n)) for n in cands)


def _stale_response(ctx: dict, reason: str, detail: str = ""):
    """A warm-routed request this node cannot serve warm: 409 BEFORE any body
    byte. Nothing is admitted, loaded or forwarded -- the router's view was
    stale, and the router (not this node) decides what to do about it."""
    ctx["route_stale"] = reason
    _note_demand(ctx, "route_stale", 409)
    return JSONResponse(status_code=409, content={
        "error": "route_stale", "reason": reason, "detail": detail,
        "node": SELF_BASE, "request_id": ctx.get("request_id")})


async def _off_loop(fn):
    """Run a blocking call (broker HTTP, peer probes) on a worker thread, so the
    loop keeps relaying the streams already in flight while it waits."""
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def _proxy_impl(path: str, request: Request, ctx: dict):
    """OpenAI-compatible surface. Every call goes through `manager.ensure`, so a
    request against an evicted unit reloads it through Harmony's admission
    (which makes room first) rather than racing the planner."""
    body = await request.body()
    # Route by the requested model, so one node can serve several. The unit is
    # ensured BEFORE the request goes anywhere: a call against an evicted unit
    # reloads it through Harmony's admission (which makes room first) rather
    # than racing the planner.
    unit = next(iter(SPECS))
    requirement = None
    prefer: "list[dict]" = []
    parsed_body = None
    regions, ctx["regions_source"] = None, "none"
    if body:
        try:
            parsed_body = json.loads(body)
        except Exception:
            parsed_body = None            # not JSON: forward untouched, as before
        if isinstance(parsed_body, dict):
            # Samples requested: absent means 1 (the OpenAI default), which is
            # known, not unknown. vLLM runs and counts each sample as its own
            # request, so without this a demand record for the hub's n=12 chip
            # call reads as one request where the engine did twelve.
            try:
                ctx["n"] = int(parsed_body.get("n") or 1)
            except (TypeError, ValueError):
                ctx["n"] = None
            # A malformed requirement is a 400 and must NOT be swallowed here.
            # `except Exception: pass` used to catch it and fall through to "the
            # first declared unit", so a caller that asked for 27B and typoed the
            # clause got a 4B model and a 200.
            requirement = _requirement_from(parsed_body)
            # `prefer` beside `harmony_requires`: an ORDERING over whatever
            # satisfies the requirement (design §4b.3), same vocabulary as
            # fleet_rank's `prefer`. Malformed is a 400 the caller can fix —
            # swallowing it would answer a request they did not ask.
            try:
                prefer = _prefer_from(parsed_body)
            except _PreferenceError as e:
                raise HTTPException(status_code=400, detail=f"harmony: {e}")
            regions, ctx["regions_source"] = _request_regions(request, parsed_body)
            derived = _derived_requirements(path, parsed_body)
            named = _model_choice(parsed_body)
            if requirement is not None and derived:
                # Derived clauses are ANDed in and may only make the query
                # STRICTER. A caller cannot declare `vision: false` to escape
                # having sent an image.
                requirement = {**requirement, **derived}
            elif requirement is None and named:
                # A CALLER THAT NAMES A UNIT HAS CHOSEN ONE.
                #
                # `derived` is non-empty for every chat request — the path
                # alone yields `class=llm` — so ANDing it in unconditionally
                # made `requirement` non-None for ALL of them, the `model`
                # field was never read, and the request was resolved as "any
                # unit of class llm", preferring one already resident.
                #
                # Measured on xc-tower-ubuntu 2026-09-18: a request naming
                # `llm_title` (a 27B) was answered by `llm_small` (a 9B) with
                # 200 OK and nothing in the exchange saying so, because the 9B
                # was the warm one. The caller's own words were discarded by
                # the machinery meant to add to them.
                #
                # So a named unit IS the selection; what the request implies is
                # CHECKED against it rather than used to re-pick. A named unit
                # that cannot do what the request needs is an error the caller
                # can act on, not a silent reroute to one that can.
                unit = named
                # Said, not refused. `_local_satisfies` treats an undeclared
                # attribute as unmet — rightly, for a search over units — but
                # a unit's attribute list is routinely thinner than the unit:
                # `llm_title` serves tools through `--enable-auto-tool-choice`
                # and declares no `tools` attribute. Turning that into a 400
                # would break working callers over a declaration gap, so the
                # caller's choice stands and the gap is printed for whoever
                # maintains the units file.
                unmet = {k: v for k, v in (derived or {}).items()
                         if not _local_satisfies(named, {k: v})}
                if unmet:
                    print(f"[harmony-llm] {named} was named explicitly and does not declare "
                          f"{unmet}, which this request implies — serving it anyway. State a "
                          f"requirement instead of a model name to have the planner choose.",
                          flush=True)
            elif requirement is None:
                requirement = derived or None
                if requirement is None:
                    unit = _unit_for_model(parsed_body.get("model", ""))
    # AN EMBEDDING NEED NO LOCAL UNIT MEETS goes to the embed node on the mesh
    # that does (fleet_embed.py). There is nothing to admit -- those nodes serve
    # from their own process -- so this runs BEFORE admission, which would
    # answer "no unit satisfies" for a node it does not place. It is a pure
    # extension: a local unit that meets the need still wins, and when no fleet
    # node meets it either, the request falls through to the refusal it always
    # got.
    if (path == "embeddings" and requirement is not None and isinstance(parsed_body, dict)
            and not any(_local_satisfies(n, requirement) for n in SPECS)):
        fleets = await asyncio.get_running_loop().run_in_executor(
            None, _fetch_fleets, BROKER_URLS, httpx.get)
        try:
            node = _pick_embed(requirement, _embed_nodes(fleets))
        except AmbiguousEmbedder as e:
            raise HTTPException(status_code=400, detail=f"harmony: {e}")
        if node:
            return await _forward_to_embed_node(node, parsed_body, request, ctx, requirement)
    # ADMISSION FIRST, then load. Loading straight off the request is how a node
    # ends up starting vLLM into whatever memory happens to be free, with 10 GB
    # of idle ASR and TTS on the card that nobody ever asked to move:
    #
    #   ValueError: Free memory on device cuda:0 (6.9/23.56 GiB) on startup is
    #   less than desired GPU memory utilization (0.62, 14.61 GiB)
    #
    # `admit` asks Harmony to make room and returns WHERE it granted. The broker
    # evicts victims and warms the grant before it answers, so by this point the
    # unit is resident on the granted device. Two consequences worth stating:
    # the placement is the planner's, not this node's, and a broker that does
    # not answer degrades to loading locally exactly as before.
    # Admission is for LOADING. A unit already resident here has been through it
    # and is serving; asking the planner for permission to use what is already
    # on the card turns a working model into a 503 on every request — observed
    # exactly that way, a benchmark refused against its own loaded model.
    already_here = unit in getattr(manager, "resident", ()) and _vllm_up(name=unit)
    # A WARM-ROUTED REQUEST IS SERVED RESIDENT-ONLY, OR REFUSED. The router's
    # view said a resident unit here would meet the request; this node is the
    # authority on whether that is still true. Anything else -- the unit was
    # evicted, is saturated, no longer meets the requirement -- is `409
    # route_stale` before any body byte, and never an admission, a load or a
    # forward: a stale view must not become a surprise model load on a node the
    # router chose for being warm. A cold-routed (or unmarked) request keeps
    # today's behaviour.
    routed_in = ctx.get("routed_in")
    warm_only = routed_in is not None and routed_in.expect == "warm"
    if warm_only:
        cands = ([n for n in _ordered(prefer) if _local_satisfies(n, requirement)]
                 if requirement is not None else [unit])
        up = [n for n in cands if n in getattr(manager, "resident", ()) and _vllm_up(name=n)]
        stale, slot = None, None
        if not cands:
            stale = "unsatisfied"
        elif not up:
            stale = "not_resident"
        else:
            # Resident + satisfies + a slot, taken as one step with no await in
            # between, and never queued behind other requests.
            for n in up:
                slot = _QUEUES.try_acquire(n, _max_concurrent(n))
                if slot is not None:
                    unit = n
                    break
            if slot is None:
                stale = "saturated"
        if stale:
            return _stale_response(ctx, stale)
        manager.units[unit].busy = True        # pinned against idle eviction while held
        ctx["queue_slot"], ctx["queue_ms"] = slot, 0.0
        try:
            ctx["in_flight"] = int(_QUEUES.status(unit, _max_concurrent(unit))["in_flight"])
        except Exception:
            ctx["in_flight"] = None
        ctx["shortcut"] = "warm-routed"
        already_here = True
    # A SPEED PREFERENCE IS A FLEET QUESTION (fleet_route.py). Everything below
    # answers within this host; a caller that asked for the fastest decode
    # wants the node whose unit has MEASURED fastest, wherever it is. Asked
    # first, so a slower resident unit here does not win on being here — the
    # ranking still puts a warm node ahead of a cold one, so a request only
    # travels to pay a load when nothing warm anywhere can serve it.
    fleet_target, fleet_expect, view = None, "cold", None
    trigger = None
    if FLEET_URL and not warm_only:
        trigger = _wants_fleet_route(
            prefer, request.headers,
            no_warm_local=not _local_ready(requirement, prefer, unit, regions))
    if trigger:
        view = await _off_loop(lambda: _fleet_view(FLEET_URL))
        if view is None:
            ctx["fleet_route"] = {"decision": "no fleet view", "fleet": FLEET_URL,
                                  "trigger": trigger, "request_id": ctx["request_id"]}
        else:
            fleet_target, ctx["fleet_route"] = _fleet_choose(
                view, requirement, prefer, SELF_BASE,
                expected_tokens=_expected_tokens(parsed_body), regions=regions)
            age = _view_age_s(FLEET_URL)
            ctx["fleet_route"].update(
                trigger=trigger, request_id=ctx["request_id"],
                regions_source=ctx.get("regions_source"),
                view_age_s=age, view_stale=bool(age is not None and age > 10.0))
            fleet_expect = ctx["fleet_route"].get("expect") or "cold"
            if (not fleet_target and regions
                    and ctx["fleet_route"]["decision"] == "no fleet target"):
                # Serving here would break the region policy, and nowhere in
                # policy can serve it: refuse, naming the rule.
                raise HTTPException(
                    status_code=503,
                    detail=f"harmony: no llm target in {'/'.join(regions)}: "
                           f"{ctx['fleet_route'].get('reason')}")
        if fleet_target:
            print(f"[harmony-llm] fleet route ({trigger}): {requirement or 'class=llm'} "
                  f"prefer {[c.get('metric') for c in prefer]} -> {fleet_target}",
                  flush=True)
            local = next((n for n in _ordered(prefer)
                          if requirement is None or _local_satisfies(n, requirement)), None)
            unit, already_here = (local or unit), False
    # A requirement is resolved by the planner — EXCEPT when a unit already
    # resident on this node satisfies it.
    #
    # `admit` conflates two questions: "which unit satisfies this?" and "where
    # may it be placed?". For a resident unit the second has already been
    # answered — it is on the card, serving. Asking again makes the planner try
    # to place a 21 GB model onto a card that same model fills, which it
    # correctly refuses: `the planner could not place it on any device`. Every
    # declarative request then 503s on a healthy node, which is precisely the
    # failure the comment above describes ("a benchmark refused against its own
    # loaded model") — reintroduced here for the requirement path.
    #
    # So: satisfied locally AND resident => serve it. Otherwise the planner
    # decides, exactly as before. Placement authority is unchanged; what is
    # removed is asking permission for a placement that already happened.
    if requirement is not None and not fleet_target and not warm_only:
        # POLICY (not an accident of the fix below): among units that satisfy
        # the requirement, REUSE ONE THAT IS ALREADY RESIDENT. An eviction and
        # reload of a 27B measured ~50.7 s on this node, and a caller that
        # stated no preference between two interchangeable units has no basis
        # to want that. Deliberately NOT a caller-settable `prefer`: the broker
        # knows residency and transition cost, the caller does not, and a knob
        # here would let one indifferent request cost everyone 50 s.
        #
        # Consequence worth stating: once an alternative is warm, indifferent
        # traffic follows it and does not swap back. Only a HARD requirement
        # that the resident unit fails will pay for a swap.
        local = next((n for n in _ordered(prefer)
                      if _local_satisfies(n, requirement)
                      and n in getattr(manager, "resident", ())
                      and _vllm_up(name=n)), None)
        # A SATURATED UNIT IS NOT A SHORTCUT (design §4a.3). The shortcut is a
        # latency optimisation for a unit with ROOM; at the engine's admission
        # limit with a queue of its own, the ROUTER decides instead — which can
        # place a sibling or move the model. The routing decision records why
        # the shortcut was skipped, so the choice is auditable either way.
        if local is not None and not _QUEUES.has_capacity(local, _max_concurrent(local)):
            ctx["shortcut"] = f"resident {local} is saturated"
            print(f"[harmony-llm] resident {local} is saturated "
                  f"({_QUEUES.status(local, _max_concurrent(local))}) — routing "
                  f"instead of reusing", flush=True)
            local = None
        if local:
            unit, already_here = local, True
        else:
            already_here = False
    # A PEER ALREADY HOLDING A SATISFYING UNIT NEEDS NO ADMISSION. Admission is
    # for LOADING (above); a copy resident on a peer of our kind was admitted
    # when it loaded. Asking again blocked every such request on the planner
    # for ~4.5 s before the forward below -- measured 2026-09-22 on
    # xc-tower-ubuntu, where every request entering via the GPU-0 node for the
    # 27B held by the GPU-1 node took 5.19 s against 0.27 s direct, and the
    # typed-decision classifier enters that way. Same holder check as before,
    # just asked first; placement authority is unchanged for anything that
    # actually has to load.
    held_peer = None
    if not already_here and not fleet_target:
        cands = ([n for n in _ordered(prefer) if _local_satisfies(n, requirement)]
                 if requirement is not None else [unit])
        for n in cands:
            h = await _off_loop(lambda n=n: _held_on_host(n))
            if h:
                unit, held_peer = n, h
                ctx["shortcut"] = "same-host peer"
                break
    granted, degraded, refused = None, None, None
    if not already_here and not held_peer and not fleet_target and (requirement is not None or len(SPECS) > 1 or MULTI_NODE):
        # WHO IS ASKING, asserted by the hub that authenticated this caller and
        # relayed here as X-Harmony-Owner (see HARMONY.md, "Who is asking").
        # The header is an ASSERTION, not a credential: this engine is reachable
        # only on the mesh, so it is trusted to relay what its hub vouched for,
        # and the fleet broker resolves the owner against THIS engine's
        # delegating token. Absent header => the caller was anonymous to the
        # hub too, and the admission is charged to the engine's own identity,
        # marked owner_asserted=False so the ledger can tell the two apart.
        asserted = (request.headers.get("x-harmony-owner") or "").strip()
        try:
            # OFF THE LOOP: admission is blocking HTTP, and the broker planning
            # it probes this very node's /livestack/residence. Run on the loop,
            # the probe cannot be answered until the broker answers — a 5.0 s
            # probe timeout per admission, during which every stream this node
            # is relaying stops being read and its engine stops decoding
            # (zz-whitby 2026-10-18; tests/test_harmony_llm_loop_blocking.py).
            def _ask():
                return admit(unit if requirement is None else "",
                             requires=requirement,
                             owner_id=asserted or f"harmony-llm:{HOST_ID}",
                             owner_asserted=bool(asserted),
                             token=_FLEET_TOKEN, timeout=ADMIT_TIMEOUT)
            res = await _off_loop(_ask)
            # DRAIN, DON'T KILL (livestack host_ledger.py): a load refused for
            # HOST MEMORY is waiting for task attempts on this machine to
            # finish, and the broker has written it to the host's ledger so no
            # new attempt is admitted meanwhile. Asking again is what keeps
            # that entry alive; giving up is what lets it lapse. Bounded by
            # HARMONY_LLM_DRAIN_WAIT_S, and the wait lands in `queue_ms`.
            drain_started = time.time()
            while (not res.get("granted") and "host memory" in str(res.get("defer_reason") or "")
                   and time.time() - drain_started < DRAIN_WAIT_S):
                if not ctx.get("drain_logged"):
                    ctx["drain_logged"] = True
                    print(f"[harmony-llm] {requirement or unit}: waiting for host RAM "
                          f"(draining task attempts): {res.get('defer_reason')}", flush=True)
                await asyncio.sleep(DRAIN_POLL_S)
                res = await _off_loop(_ask)
            if ctx.get("drain_logged"):
                ctx["drain_ms"] = round((time.time() - drain_started) * 1000, 1)
            served = res.get("kind")
            # A loading transition temporarily withholds this facade's fleet
            # registration. During that gap the broker can answer "nothing
            # satisfies" even though this process's static catalogue declares
            # the requested unit. Falling back only to a LOCAL exact match is
            # safe: the manager still enforces coload policy before loading, so
            # an exclusive single-GPU deployment evicts its other local unit.
            # This is not a placement guess for an arbitrary peer.
            # ONLY WHEN THE BROKER DID NOT KNOW THE UNIT. On 2026-09-30 the
            # broker answered "no device can fit even with preemption" (an
            # image model held the card), this fallback loaded the 27B anyway,
            # the start failed, the in-flight load withheld this node's
            # registration, the broker then answered "no unit satisfies", and
            # the fallback fired again: 346 doomed starts, llm_general down
            # 05:29-15:42. A refusal of a unit the broker KNOWS is final here.
            # ...and "no unit satisfies" for a requirement one of OUR units
            # meets is ALSO "did not know": the facade blocks while it serves,
            # so a load drops the registration and the broker's snapshot loses
            # this node entirely (measured 2026-10-02: a re-route to a
            # locally-known flash_next died as a 503 while the broker's world
            # held no tower-llm units at all). The local check below stays the
            # discriminator: when no local unit satisfies either, the refusal
            # is real and final.
            _stale = "no unit satisfies" in str(res.get("defer_reason") or "")
            if (requirement is not None and not served
                    and (_broker_did_not_know(res) or _stale)):
                local_declared = next((n for n in _ordered(prefer)
                                       if _local_satisfies(n, requirement)), None)
                if local_declared:
                    print(f"[harmony-llm] broker temporarily forgot {requirement}; "
                          f"using locally declared {local_declared}", flush=True)
                    served = local_declared
                    res = {**res, "kind": served, "granted": True,
                           "device_id": DEVICE_ID_SELF, "reason": None}
            # Log the planner's answer only when it did NOT grant. A refusal
            # for a requirement is otherwise invisible: the caller gets a 503
            # naming what it asked for, and nothing says what the planner
            # decided or why. That gap cost an afternoon.
            if requirement is not None and not res.get("granted"):
                print(f"[harmony-llm] admit(requires={requirement}) refused -> "
                      f"kind={res.get('kind')!r} reason={res.get('reason')!r}", flush=True)
            if requirement is not None:
                if not served:
                    # ...so the planner's OWN words ride the refusal: "no unit
                    # satisfies {...}" and "residency floor: llm_general loaded
                    # 37s ago is protected for 60s" are different answers and
                    # cost different actions (change the request vs wait).
                    why = str(res.get("defer_reason") or res.get("reason") or "")
                    raise HTTPException(
                        status_code=503,
                        detail=(f"nothing satisfies {requirement}"
                                + (f" — {why}" if why else "")))
                # WHICH unit a requirement resolved to, every time it changes
                # the answer. Only refusals were logged, so a grant that chose
                # a different model than the caller had in mind left no trace
                # at all — the whole reason it took a response body's `model`
                # field to notice a 27B request being served by a 9B.
                if served != unit:
                    print(f"[harmony-llm] {requirement} -> {served}"
                          + (f" (request named {unit})" if unit else ""), flush=True)
                unit = served
            granted, degraded = res.get("device_id"), res.get("degraded")
            # A broker that ANSWERED and did not grant has refused. Loading
            # anyway is what admission exists to stop: it puts a model on a card
            # the planner never cleared, which is an OOM at best and someone
            # else's evicted model at worst.
            if not degraded and not res.get("granted"):
                refused = res.get("reason") or "the planner did not grant a device"
                # The planner's own why, and how long the drain was waited out
                # — "no room" alone cannot tell a caller it queued behind batch.
                if res.get("defer_reason"):
                    refused += f" ({res['defer_reason']})"
                if ctx.get("drain_ms") is not None:
                    refused += (f"; waited {ctx['drain_ms'] / 1000:.0f} s for task "
                                f"attempts to drain (HARMONY_LLM_DRAIN_WAIT_S)")
        except HTTPException:
            raise                             # a refusal we raised ourselves, not a fault
        except Exception as e:                # unreachable: arbitration is not the model
            degraded = f"{type(e).__name__}: {e}"
    if degraded and requirement is not None:
        # Degrading means "proceed without arbitration", and there is no such
        # thing for a REQUIREMENT: which model satisfies it is precisely what we
        # could not ask. Serving whatever this node happens to hold would answer
        # a different question than the caller asked.
        raise HTTPException(
            status_code=503,
            detail=f"cannot resolve {requirement}: arbitration unavailable ({degraded})")
    if degraded:
        print(f"[harmony-llm] admission unavailable for {unit} "
              f"({degraded}) — loading without it", flush=True)
    if refused:
        raise HTTPException(status_code=503,
                            detail=f"{unit} was not admitted: {refused}")

    elsewhere = fleet_target or held_peer
    if granted and DEVICE_ID_SELF and granted != DEVICE_ID_SELF:
        elsewhere = _peer_at(granted)
        if elsewhere is None:
            raise HTTPException(
                status_code=503,
                detail=f"{unit} was placed on {granted}, which is not this node "
                       f"and has no reachable peer")
    # USE THE COPY THAT EXISTS. The planner was asked "where may I put this?"
    # and answers with a card that is free — for a node whose own card is empty,
    # that is its own card, every time. It is a correct answer to the wrong
    # question: a unit already resident (or loading) on a peer of our kind needs
    # no placement at all, and loading a second copy wastes a whole card to
    # serve what the host already serves. Observed as two 21.7 GB copies of one
    # 27B across two 3090s, leaving nowhere to put a 3 GB embedding unit.
    #
    # After the `granted` branch, so an explicit placement still wins; before
    # `manager.ensure`, which is the load this avoids. `already_here` short-
    # circuits it, so a node serving the unit itself never pays for this.
    if not elsewhere and not already_here:
        holder = await _off_loop(lambda: _held_on_host(unit))
        if holder:
            ctx["shortcut"] = "same-host peer"
            print(f"[harmony-llm] {unit} is held by {holder} — forwarding "
                  f"rather than loading a second copy", flush=True)
            elsewhere = holder

    foreign = not elsewhere and not already_here and _foreign_listener(unit)
    if foreign:
        print(f"[harmony-llm] {unit}: port {SPECS[unit]['port']} is served by a vLLM this "
              f"node did not start -- forwarding to it rather than loading, which would "
              f"evict this node's residents. Two nodes share this port: give each its own "
              f"HARMONY_LLM_PORT_OFFSET.", flush=True)

    ctx["unit"] = unit
    ctx["requirement_hash"] = requirement_hash(requirement)
    ctx["forwarded"] = bool(elsewhere or foreign)
    if prefer:
        _k, ctx["preference_receipt"] = _prefer_key(unit, prefer)
    # PER-UNIT ADMISSION QUEUE (design §4a): the engine admits `max_concurrent`
    # requests; the rest wait FIFO (bound 64), and the overflow is a 429 naming
    # the queue state — never an unbounded pile-up inside a saturated engine.
    # `queue_ms` (how long this one waited) goes on the demand record.
    # OFF THE EVENT LOOP: `acquire` BLOCKS while the engine is at its limit
    # (that is the queue), and a blocking wait inside an async handler wedges
    # the whole node — every other request, including /livestack/residence,
    # stops answering (measured 2026-10-02: residence:000 with the engine
    # idle). The wait is real; it just must not hold the loop.
    import asyncio as _asyncio
    # A request the FLEET placed elsewhere is that node's to queue: holding a
    # place in a local unit's queue for it would make this node's own unit
    # look busy (and one slot tighter) while serving nothing.
    if not fleet_target and not warm_only:
        try:
            _slot = await _asyncio.get_running_loop().run_in_executor(
                None, _QUEUES.acquire, unit, _max_concurrent(unit))
        except _QueueFull as e:
            raise HTTPException(status_code=429, detail=str(e))
        ctx["queue_ms"] = _slot.queue_ms
        ctx["queue_slot"] = _slot
        # How many requests this unit is running, this one included: a batched
        # engine decodes each stream slower, and the speed evidence must say which.
        try:
            ctx["in_flight"] = int(_QUEUES.status(unit, _max_concurrent(unit))["in_flight"])
        except Exception:
            ctx["in_flight"] = None
        # The queue brackets the in-flight window, so it is the unit's busy truth:
        # a co-tenant acquire refuses to evict a unit that is serving (idle-only
        # preemption, as the planner has it).
        try:
            manager.units[unit].busy = True
        except Exception:
            pass
    if elsewhere:
        _busy.acquire()
        url = f"{elsewhere}/v1/{path}"
    elif foreign:
        _busy.acquire()
        url = f"{_base_of(unit)}/v1/{path}"
    else:
        try:
            _ensure_while_counted(lambda: manager.ensure(unit))
        except Exception as e:
            _release_slot(ctx)
            raise HTTPException(status_code=503, detail=f"{unit} unavailable: {e}")
        url = f"{_base_of(unit)}/v1/{path}"

    # NORMALIZE before forwarding. Resolving a unit is not the same as ASKING it
    # for what the requirement implied, and the gap between those two is a
    # silent failure: `model: "require:..."` reaches a backend whose served
    # names do not include it, and a requirement that selected a
    # thinking-capable unit never actually turns thinking ON.
    # A fleet-routed request travels AS ASKED: the receiving node resolves the
    # requirement against its own units. Normalising it here rewrote `model`
    # to THIS node's unit, and the receiver was asked for a unit by name
    # (zz-whitby -> tower 2026-10-18: a 27B request arrived as `flash_next`).
    # A same-host peer forward is the same: the peer is a facade with its own
    # unit table, so it gets the requirement, not this node's idea of it.
    if (not elsewhere and isinstance(parsed_body, dict)
            and (requirement is not None or "harmony_requires" in parsed_body)):
        out = dict(parsed_body)
        out.pop("harmony_requires", None)          # ours, not the backend's
        out.pop("harmony_regions", None)           # policy for the router, not the engine
        served = SPECS.get(unit, {}).get("model") or unit
        if str(out.get("model", "")).startswith("require:") or requirement is not None:
            out["model"] = served
        # vLLM selects a LoRA by serving it under the adapter's own name. A
        # requirement that asked for an adapter and was then sent to the base
        # model's name would get the base weights and a 200 -- the exact silent
        # substitution this normalisation exists to prevent.
        wanted = [k[len("adapter."):] for k in (requirement or {}) if k.startswith("adapter.")]
        if len(wanted) > 1:
            # This runs AFTER the in-flight places were taken; a refusal that
            # holds them would make the node look busy (and its queue one slot
            # tighter) for a request that has already ended.
            _busy.release()
            _release_slot(ctx)
            raise HTTPException(status_code=400,
                                detail=f"harmony: one request can use one adapter, asked for {wanted}")
        if wanted:
            out["model"] = wanted[0]
            ctx["adapter"] = wanted[0]
        # The parameter the requirement implied. Asking for `thinking` and then
        # not sending `enable_thinking` gets a capable unit that does not think.
        if requirement.get("thinking") is True if requirement else False:
            kw = dict(out.get("chat_template_kwargs") or {})
            kw.setdefault("enable_thinking", True)
            out["chat_template_kwargs"] = kw
        body = json.dumps(out).encode()

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length"}}
    headers["x-request-id"] = ctx["request_id"]
    timeout = httpx.Timeout(float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")))
    if fleet_target:
        # PLACED by the fleet: send marked routed (the receiver never re-routes),
        # with what we expect of it. One retry on a refusal that cost the caller
        # no byte (fleet_route.send_routed); never after streaming starts.
        attempts: list = []
        ctx["fleet_route"]["attempts"] = attempts

        def _rechoose(tried, _view=view):
            tgt, rec = _fleet_choose(_view, requirement, prefer, SELF_BASE,
                                     expected_tokens=_expected_tokens(parsed_body),
                                     regions=regions, exclude=tried)
            return tgt, (rec.get("expect") or "cold")

        ctx["sent_at"] = time.time()
        try:
            resp, client = await _send_routed(
                first=(fleet_target, fleet_expect), rechoose=_rechoose,
                router_id=HOST_ID, request_id=ctx["request_id"],
                method=request.method, path=path, body=body, headers=headers,
                params=dict(request.query_params), timeout=timeout, attempts=attempts,
                on_stale=lambda t: _mark_cold(FLEET_URL, t))
        except _RouteFailed as e:
            _busy.release()
            _release_slot(ctx)
            raise HTTPException(status_code=e.status,
                                detail={"error": "route_failed", "attempts": e.attempts})
    else:
        client = httpx.AsyncClient(timeout=timeout)
        try:
            req = client.build_request(request.method, url, content=body, headers=headers,
                                       params=dict(request.query_params))
            ctx["sent_at"] = time.time()          # first-token time starts here, not at the queue
            resp = await client.send(req, stream=True)
        except Exception as e:
            await client.aclose()
            _busy.release()
            _release_slot(ctx)
            # The TYPE is part of the answer: `Server disconnected without sending
            # a response` and `All connection attempts failed` are different
            # failures, and an empty message carried neither (this is how the
            # acceptance's first 502 said nothing at all).
            raise HTTPException(status_code=502,
                                detail=f"proxy to {unit} failed: "
                                       f"{type(e).__name__}: {e or '(no message)'}")

    # A CONTEXT REFUSAL IS A ROUTING FACT, NOT A VENDOR STRING.
    #
    # vLLM answers an over-long prompt with a 400 whose text names the numbers
    # exactly: "maximum context length is 16384 tokens ... your prompt contains
    # at least 16385 input tokens". Streamed straight through, that arrived in a
    # user-visible chat bubble as a raw upstream error, and diagnosing it by
    # hand on 2026-09-08 took reading vLLM logs to discover the Overlord's
    # prompt (system text plus ~46 tool schemas) had outgrown the unit's window
    # by ONE token.
    #
    # Harmony cannot derive this need up front — token counts depend on the
    # candidate model's tokenizer and template, which is why
    # docs/livestack-harmony.md refuses to gate on a body-size estimate. But it
    # does not have to estimate: the unit MEASURED it and said so. So the answer
    # is restated in Harmony's own terms, naming the need and whether anything
    # on this node could serve it — the difference between "the vendor said no"
    # and "you asked for more context than this node has".
    if resp.status_code == 400:
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
            _busy.release()
            _release_slot(ctx)
        text = raw.decode("utf-8", "replace")
        # WHETHER this refusal means "the prompt is too long" is the ENGINE's
        # dialect, not a string this server knows: vLLM says "maximum context
        # length is 16384 tokens ... at least 16385 input tokens", llama.cpp
        # says "prompt (144950 tokens) + max tokens (16) exceeds the context
        # (131072)". The adapter answers `context_refusal` — the seam that
        # exists for exactly this (measured 2026-10-02: the block matched only
        # vLLM's words and a llama.cpp refusal fell through to a raw passthrough,
        # never the 413 with the need named).
        _ctx_refusal = (_engine_for(SPECS.get(unit) or {})
                        .context_refusal(400, raw)) if unit in SPECS else None
        if not _ctx_refusal:
            _t = UsageTail()
            _t.feed(raw)
            _note_demand(ctx, "refused", 400, _t)
        if _ctx_refusal:
            needed = None
            m = re.search(r"at least (\d+) input tokens", text)
            if not m:
                m = re.search(r"prompt \((\d+) tokens\)", text)
            if m:
                # +1: a prompt of exactly N tokens needs room for N, and the
                # message reports a floor ("at least"), never a ceiling.
                needed = int(m.group(1))
            widest, widest_name = 0, None
            for name in SPECS:
                served = _attributes_for(SPECS[name]).get("context_len") or 0
                if int(served) > widest:
                    widest, widest_name = int(served), name
            # The window must hold the prompt AND the reserved output, so the
            # need is input + max_tokens. The first version of this message
            # compared `needed` against the widest window alone, decided a wider
            # unit "exists", and named the very unit that had just refused —
            # because 24561 input fits 24576 and 24561 + 16 does not.
            reserve = 0
            if isinstance(parsed_body, dict):
                try:
                    reserve = int(parsed_body.get("max_tokens") or 0)
                except (TypeError, ValueError):
                    reserve = 0
            total = None if needed is None else needed + reserve
            # A RE-ROUTE, ONCE (design §4c): when ANOTHER unit can hold the
            # need, state the need as a REQUIREMENT and re-run admission — the
            # only loop-safe way to re-route. Never pick a unit by name (the
            # planner chooses, and may move the model), and never loop: one
            # re-route per request, and the next context refusal is answered as
            # it comes. When nothing else can hold it, the 413 below stands.
            # ONLY FOR AN UN-NAMED REQUEST (task 3.5): a caller that NAMED a
            # unit chose it and gets the 413 — a silent re-route to some other
            # model they did not ask for is exactly what "named is named"
            # forbids. `requirement` is set exactly when the caller stated a
            # NEED (or named nothing, where the path implies the class).
            if requirement is not None and isinstance(parsed_body, dict) \
                    and total is not None and not ctx.get("context_rerouted"):
                # The need ANDed into the caller's OWN clauses (a unit that can
                # hold the prompt but not the adapter is no answer), and only
                # making the query STRICTER.
                want = {**(requirement or {}), "context_len>=": total}
                if any(n != unit and _local_satisfies(n, want) for n in SPECS):
                    ctx["context_rerouted"] = True
                    print(f"[harmony-llm] context refusal on {unit} ({total} tokens) "
                          f"— re-routing once to require:class=llm,context_len>={total}",
                          flush=True)
                    rerouted = dict(parsed_body)
                    rerouted.pop("harmony_requires", None)
                    rerouted["harmony_requires"] = want
                    return await _proxy_impl(
                        path, _Retargeted(request, json.dumps(rerouted).encode()), ctx)
            served_here = int(_attributes_for(SPECS[unit]).get("context_len") or 0) if unit in SPECS else 0
            wider = widest > served_here
            if total is None:
                detail = f"{unit} refused this request's context length: {text.strip()[:200]}"
            elif not wider:
                detail = (f"this request needs {total} tokens ({needed} input + {reserve} reserved "
                          f"for output); the widest unit on this node is {unit} at {served_here}. "
                          f"Nothing here can satisfy it — raise that unit's max_model_len (bounded "
                          f"by its KV cache) or send less.")
            else:
                detail = (f"this request needs {total} tokens ({needed} input + {reserve} reserved "
                          f"for output) and was served by {unit} at {served_here}. {widest_name} "
                          f"serves {widest} — state the need, e.g. "
                          f"require:class=llm,context_len>={total}.")
            print(f"[harmony-llm] context refusal on {unit}: needed={needed} widest={widest}",
                  flush=True)
            raise HTTPException(status_code=413, detail=detail)
        # Any other 400 is the caller's own and passes through unchanged.
        async def replay_400():
            yield raw
        return StreamingResponse(
            replay_400(), status_code=400,
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() not in {"content-length", "transfer-encoding"}},
        )

    async def body_iter():
        # The request is in flight until the LAST byte has been streamed to the
        # caller, not until the upstream accepted it — a generation that is still
        # producing tokens is still occupying the card.
        tail = UsageTail()
        try:
            async for chunk in resp.aiter_raw():
                tail.feed(chunk)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            _busy.release()
            _release_slot(ctx)
            _note_demand(ctx, "ok" if resp.status_code < 400 else "refused",
                         resp.status_code, tail)

    return StreamingResponse(
        body_iter(), status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in {"content-length", "transfer-encoding"}},
    )


if __name__ == "__main__":
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            _SHUTDOWN.set()
            super().handle_exit(sig, frame)

    # Without a handler SIGHUP would terminate the process.
    signal.signal(signal.SIGHUP, lambda *_: _reload_classifier_principals())
    _Server(uvicorn.Config(app, host="0.0.0.0", port=NODE_PORT)).run()
