"""What a vLLM engine SAID it costs, parsed from its own startup lines.

A unit's memory cost used to be a number an operator typed (`footprint_gb`).
On 2026-09-28 that number was 21 GB while the engine reported a 22.62 GiB budget
plus 0.90 GiB of CUDA graphs outside it, and a second LoRA adapter was estimated
at ~0.45 GiB and measured at ~1 GiB. The engine prints the truth at startup;
this module reads it.

Pure: text in, `MeasuredCost` out. `composition_hash` is here too because a
measurement is only meaningful for the exact composition that produced it, and
both harmony-llm (which measures) and the host broker (which proposes) must
compute the same id without trusting each other's bookkeeping.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping, Optional

GIB = float(1 << 30)

# Every line the parser needs, by name. A name missing from the result is
# reported as unmatched, so an engine that changed its log format reads as
# `unknown: [kv_cache_size]`, never as a zero.
_PATTERNS: Mapping[str, re.Pattern] = {
    "engine": re.compile(r"Initializing a V1 LLM engine \(v(?P<version>[^)]+)\)"),
    "model_loading": re.compile(r"Model loading took (?P<gib>[\d.]+) GiB"),
    "kv_cache_memory": re.compile(r"Available KV cache memory: (?P<gib>[\d.]+) GiB"),
    "kv_cache_size": re.compile(
        r"GPU KV cache size: (?P<tokens>[\d,]+) tokens, Maximum concurrency for "
        r"(?P<ctx>[\d,]+) tokens per request: (?P<conc>[\d.]+)x"),
    # Hybrid (attention + linear-attention) models: vLLM sizes the attention
    # block so one attention page equals one state page, and every page in the
    # pool is that many tokens. 784 with bf16 KV and 1568 with fp8 KV on the
    # Qwen3.8 27B. Absent on plain-attention models (block stays small).
    "block_size": re.compile(r"Setting attention block size to (?P<tokens>\d+) tokens"),
    "cuda_graphs": re.compile(r"Graph capturing finished in [\d.]+ secs?, took (?P<gib>[\d.]+) GiB"),
    "actual_usage": re.compile(
        r"Desired GPU memory utilization is \((?P<frac>[\d.]+), (?P<budget>[\d.]+) GiB\)\. "
        r"Actual usage is (?P<consumed>[\d.]+) GiB for consumed memory \(weights \+ non-torch\), "
        r"(?P<act>[\d.]+) GiB for peak activation, and (?P<graphs>[\d.]+) GiB for CUDAGraph memory"),
}
# Lines whose absence makes the measurement unusable. `engine` and
# `model_loading` are informative; the cost itself comes from these.
REQUIRED = ("kv_cache_memory", "kv_cache_size", "actual_usage")


@dataclass(frozen=True)
class MeasuredCost:
    """Bytes, from the engine. `cuda_graphs` sits OUTSIDE `budget` in vLLM's
    accounting (the 0.96 fraction covers weights + activation + KV), which is
    why the footprint below adds it rather than trusting the budget alone."""
    weights_nontorch: int
    peak_activation: int
    cuda_graphs: int
    kv_bytes: int
    kv_tokens: int
    max_concurrency: float
    max_concurrency_at: int
    budget: int
    gpu_fraction: float
    engine_version: str = ""
    composition_hash: str = ""
    measured_at: float = 0.0
    source: str = "vllm-startup"
    # Tokens per KV page (0 = the engine did not say: not a hybrid model).
    block_size: int = 0
    # State pages each running SEQUENCE holds beyond its attention pages,
    # FITTED from the engine's own stats lines (`replay_validate --fit-state`),
    # not from the startup log. 0 = not fitted yet.
    state_pages_per_seq: float = 0.0
    # Engine service rates, FITTED the same way: total prompt (prefill)
    # throughput, and decode tokens/s per sequence. 0 = not fitted.
    prefill_tok_s: float = 0.0
    decode_tok_s: float = 0.0

    @property
    def footprint(self) -> int:
        """What the card really holds for this unit while it is resident."""
        return self.weights_nontorch + self.peak_activation + self.kv_bytes + self.cuda_graphs

    @property
    def min_footprint(self) -> int:
        """The least the engine needs: everything but the ELASTIC part of the KV
        cache. vLLM sizes KV to fill its budget, so `footprint` is as large as
        the grant, not a requirement; what it cannot run without is KV for one
        request of the length it was started with."""
        per_token = self.kv_bytes / self.kv_tokens if self.kv_tokens else 0
        return int(self.weights_nontorch + self.peak_activation + self.cuda_graphs
                   + per_token * self.max_concurrency_at)

    def to_json(self) -> dict:
        out = asdict(self)
        out["footprint"] = self.footprint
        out["min_footprint"] = self.min_footprint
        return out


@dataclass(frozen=True)
class Unknown:
    """The engine became ready but its memory report did not parse.
    `unmatched` names the lines that were missing. Never read as 0."""
    unmatched: tuple
    engine_version: str = ""
    composition_hash: str = ""
    measured_at: float = 0.0
    source: str = "unknown"

    def to_json(self) -> dict:
        return {"measured": "unknown", "unmatched": list(self.unmatched),
                "engine_version": self.engine_version,
                "composition_hash": self.composition_hash,
                "measured_at": self.measured_at}


class StartupCapture:
    """Feed engine output line by line; keep only what the parser needs.

    Bounded: it retains at most one line per pattern, so an engine that logs
    for hours costs nothing here."""

    def __init__(self) -> None:
        self._hits: dict = {}

    def feed(self, line: str) -> None:
        for name, pat in _PATTERNS.items():
            m = pat.search(line)
            if m:
                self._hits[name] = m.groupdict()

    def result(self, *, composition_hash: str = "", now: float = 0.0):
        return _build(self._hits, composition_hash=composition_hash, now=now)


def parse(lines: Iterable[str], *, composition_hash: str = "", now: float = 0.0):
    cap = StartupCapture()
    for line in lines:
        cap.feed(line)
    return cap.result(composition_hash=composition_hash, now=now)


def _gib(v: str) -> int:
    return int(round(float(v) * GIB))


def _build(h: Mapping[str, Mapping[str, str]], *, composition_hash: str, now: float):
    version = (h.get("engine") or {}).get("version", "")
    missing = tuple(n for n in REQUIRED if n not in h)
    if missing:
        return Unknown(unmatched=missing, engine_version=version,
                       composition_hash=composition_hash, measured_at=now)
    usage, size = h["actual_usage"], h["kv_cache_size"]
    return MeasuredCost(
        weights_nontorch=_gib(usage["consumed"]),
        peak_activation=_gib(usage["act"]),
        cuda_graphs=_gib(usage["graphs"]),
        kv_bytes=_gib(h["kv_cache_memory"]["gib"]),
        kv_tokens=int(size["tokens"].replace(",", "")),
        max_concurrency=float(size["conc"]),
        max_concurrency_at=int(size["ctx"].replace(",", "")),
        budget=_gib(usage["budget"]),
        gpu_fraction=float(usage["frac"]),
        engine_version=version,
        composition_hash=composition_hash,
        measured_at=now,
        block_size=int((h.get("block_size") or {}).get("tokens") or 0),
    )


@dataclass(frozen=True)
class CompositionKey:
    """The launch facts that decide a unit's memory, and nothing else.

    Two units with the same key cost the same on the same card; change any
    field and the old measurement no longer applies."""
    base: str
    adapters: tuple = ()            # ((name, rank), ...) — sorted by name
    kv_dtype: str = "auto"
    max_model_len: int = 0
    max_num_seqs: int = 0
    engine_version: str = ""
    extra: tuple = field(default_factory=tuple)   # other memory-relevant flags, sorted

    def canonical(self) -> str:
        return json.dumps({
            "base": self.base,
            "adapters": sorted([list(a) for a in self.adapters]),
            "kv_dtype": self.kv_dtype or "auto",
            "max_model_len": int(self.max_model_len or 0),
            "max_num_seqs": int(self.max_num_seqs or 0),
            "engine_version": self.engine_version,
            "extra": sorted(list(self.extra)),
        }, sort_keys=True, separators=(",", ":"))


def composition_hash(key: CompositionKey) -> str:
    return "sha256:" + hashlib.sha256(key.canonical().encode()).hexdigest()


def _flag(args: list, name: str) -> Optional[str]:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def key_from_launch(model: str, args: list, adapters: Mapping[str, int],
                    engine_version: str = "") -> CompositionKey:
    """The key for a `vllm serve` launch line. `adapters` is name -> rank, as
    harmony-llm already resolves it from each adapter's own config."""
    return CompositionKey(
        base=model,
        adapters=tuple(sorted((str(n), int(r)) for n, r in adapters.items())),
        kv_dtype=_flag(args, "--kv-cache-dtype") or "auto",
        max_model_len=int(_flag(args, "--max-model-len") or 0),
        max_num_seqs=int(_flag(args, "--max-num-seqs") or 0),
        engine_version=engine_version,
    )
