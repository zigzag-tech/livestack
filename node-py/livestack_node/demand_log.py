"""demand_log.py — what each unit and adapter actually served, one record per request.

Composition (which base, adapters, KV dtype and context a card should hold) can
only be scored against demand it can see. Before this log the only history was
vLLM's 10-second `Running/Waiting` journal lines: enough to see that requests
queued at 91-100% KV usage, not which adapter or caller they were or how long
their prompts ran. See openspec change `harmony-placement-foundation`, spec
`inference-demand-log`.

Rules this module enforces, not merely documents:

* **Bounded by size AND age, failing closed.** Storage is a `JsonlLedger`
  (size x files, age sweep on rotation). An unset age window DISABLES the log
  and says so; it never means "keep forever" (benchday rule 10).
* **Never on the request path.** `record()` puts onto a bounded queue and
  returns. One drain thread writes. A full queue drops the record and counts
  it, and the count is reported, so a drop is never silent.
* **Unknown is null.** A token count the node could not read is `None`, never 0.
* **Applications, not people.** The record keeps the principal NAMESPACE
  (`benchday:`) and the authenticated principal's name (`benchday-hub`), never
  the owner id.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from typing import Callable, Optional

from .ledger import JsonlLedger

_TAIL_BYTES = 16 * 1024
_PROMPT = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_COMPLETION = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')
# llama.cpp's `timings` (Strata emits it on every answer): the engine's own
# decode and prefill rates, measured inside the engine — better evidence than
# anything a proxy can time.
_DECODE_RATE = re.compile(rb'"predicted_per_second"\s*:\s*([0-9.]+)')
# The first chunk that carries generated text (answer or reasoning) — what a
# caller waits for. A role-only opening delta is not a token.
_FIRST_TEXT = re.compile(rb'"(?:content|reasoning|reasoning_content)"\s*:\s*"[^"]')
# Fewer generated tokens than this and a stream-timed rate is mostly noise.
_MIN_TIMED_TOKENS = 8


def owner_namespace(owner: Optional[str]) -> Optional[str]:
    """`benchday:acct_1` -> `benchday:`. An unprefixed owner (the pre-R.4 form)
    has no namespace to report, so the answer is None, not a guess."""
    owner = (owner or "").strip()
    if ":" not in owner:
        return None
    return owner.split(":", 1)[0] + ":"


def requirement_hash(requirement) -> Optional[str]:
    if not requirement:
        return None
    raw = json.dumps(requirement, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


class UsageTail:
    """The last bytes of a proxied response, to read `usage` from without
    buffering the body. vLLM puts `usage` at the end of a JSON reply and in the
    final chunk of a stream that asked for it; anything else yields None."""

    def __init__(self, limit: int = _TAIL_BYTES) -> None:
        self._buf = b""
        self._limit = limit
        self.streamed: Optional[bool] = None
        self.first_text_at: Optional[float] = None
        self.last_at: Optional[float] = None

    def feed(self, chunk: bytes, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if self.streamed is None and chunk.strip():
            self.streamed = chunk.lstrip().startswith(b"data:")
        if self.first_text_at is None and self.streamed and _FIRST_TEXT.search(chunk):
            self.first_text_at = now
        self.last_at = now
        self._buf = (self._buf + chunk)[-self._limit:]

    def tokens(self):
        def last(pat):
            found = pat.findall(self._buf)
            return int(found[-1]) if found else None
        return last(_PROMPT), last(_COMPLETION)

    def speed(self, sent_at: Optional[float] = None) -> dict:
        """`{decode_tok_s, first_token_ms, speed_source}` for this response,
        each None when it cannot be known — never a guess.

        Decode rate: the engine's own `timings.predicted_per_second` when the
        engine reports one; otherwise, for a STREAM, generated tokens after the
        first over the time from the first text chunk to the last chunk. A
        non-streamed answer arrives all at once, so the proxy cannot separate
        prefill from decode in it and says nothing. First token: a stream's
        first text chunk minus `sent_at` (when the request reached the engine,
        so a queue wait is not counted as the engine's latency)."""
        found = _DECODE_RATE.findall(self._buf)
        decode, source = None, None
        if found:
            try:
                decode, source = float(found[-1]), "engine-timings"
            except ValueError:
                decode = None
        if decode is None and self.streamed and self.first_text_at and self.last_at:
            _, completion = self.tokens()
            span = self.last_at - self.first_text_at
            if completion and completion >= _MIN_TIMED_TOKENS and span > 0:
                decode, source = (completion - 1) / span, "stream-timed"
        first = None
        if self.streamed and self.first_text_at and sent_at:
            first = max(0.0, (self.first_text_at - sent_at) * 1000)
        return {"decode_tok_s": round(decode, 2) if decode and decode > 0 else None,
                "first_token_ms": round(first, 1) if first is not None else None,
                "speed_source": source}


class SpeedStats:
    """What a unit has MEASURED itself doing, per request: decode tokens/s and
    time to first token, for `prefer` (`llm.decode_tok_s`, `llm.first_token_ms`).

    Each sample carries how many requests the unit had in flight when it ran.
    A batched engine decodes each stream slower than a solo one, so a solo and a
    burst rate are different numbers and averaging them describes neither: the
    published figure is the median of SOLO samples (cohort `solo`), and only a
    unit that has never run alone publishes the all-samples median under cohort
    `any`. Medians, so one stalled request cannot move the evidence. The sample
    count is real, so `min_samples` in a preference means what it says."""

    def __init__(self, window: int = 200) -> None:
        self._window = window
        self._by_unit: "dict[str, list[tuple[float, Optional[float], Optional[float], int]]]" = {}
        self._lock = threading.Lock()

    def observe(self, unit: str, decode_tok_s: Optional[float],
                first_token_ms: Optional[float], in_flight: int = 1,
                ts: Optional[float] = None) -> None:
        if not unit or (decode_tok_s is None and first_token_ms is None):
            return
        with self._lock:
            rows = self._by_unit.setdefault(unit, [])
            rows.append((time.time() if ts is None else ts, decode_tok_s,
                         first_token_ms, max(1, int(in_flight or 1))))
            del rows[:-self._window]

    def summary(self, unit: str) -> dict:
        """The `fitted`-shaped row `selection.unit_inventory` reads, plus the
        cohort and the burst rate for an explanation. Empty when nothing was
        measured — silence, never a zero."""
        with self._lock:
            rows = list(self._by_unit.get(unit) or [])
        if not rows:
            return {}
        solo = [r for r in rows if r[3] <= 1]
        use, cohort = (solo, "solo") if solo else (rows, "any")
        out: dict = {"cohort": cohort, "measured_at": max(r[0] for r in use)}
        for i, key in ((1, "decode_tok_s"), (2, "first_token_ms")):
            vals = [r[i] for r in use if r[i] is not None]
            if vals:
                out[key] = _median(vals)
                out[f"{key}_samples"] = len(vals)
        burst = [r[1] for r in rows if r[3] > 1 and r[1] is not None]
        if burst:
            out["decode_tok_s_burst"] = _median(burst)
            out["decode_tok_s_burst_samples"] = len(burst)
        return out

    def seed(self, path: str, max_bytes: int = 4 * 1024 * 1024) -> int:
        """Re-read measured rows from a demand log, so a restart does not wipe
        the evidence a node earned. Returns how many samples were read."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - max_bytes))
                lines = fh.read().splitlines()
        except OSError:
            return 0
        n = 0
        for line in lines:
            if b"decode_tok_s" not in line and b"first_token_ms" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rec = rec.get("record", rec) if isinstance(rec, dict) else {}
            if rec.get("outcome") not in (None, "ok"):
                continue
            if rec.get("decode_tok_s") is None and rec.get("first_token_ms") is None:
                continue
            self.observe(str(rec.get("unit") or ""), rec.get("decode_tok_s"),
                         rec.get("first_token_ms"), int(rec.get("in_flight") or 1),
                         ts=float(rec.get("ts") or time.time()))
            n += 1
        return n


def _median(vals: "list[float]") -> float:
    s = sorted(vals)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 2)


class DemandLog:
    def __init__(self, path: str, *, max_age_s: Optional[float],
                 max_bytes: int = 64 * 1024 * 1024, max_files: int = 8,
                 queue_max: int = 4096,
                 log: Callable[[str], None] = lambda *_: None) -> None:
        self.path = path
        self.dropped = 0
        self.written = 0
        self.failed = 0
        self._log = log
        if not max_age_s:
            self.enabled = False
            self.reason = "disabled: no age window"
            self._ledger = None
            self._q = None
            log(f"[demand-log] {self.reason} (set HARMONY_DEMAND_LOG_AGE_DAYS)")
            return
        self.enabled = True
        self.reason = ""
        self._ledger = JsonlLedger(path, max_bytes=max_bytes, max_files=max_files,
                                   max_age_s=max_age_s, log=log)
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=queue_max)
        threading.Thread(target=self._drain, name="demand-log", daemon=True).start()

    def record(self, **fields) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait(fields)
        except queue.Full:
            self.dropped += 1

    def _drain(self) -> None:
        while True:
            rec = self._q.get()
            if self._ledger.append_record(rec):
                self.written += 1
            else:
                self.failed += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Test helper: wait until everything queued so far is written."""
        if not self.enabled:
            return
        import time
        deadline = time.time() + timeout
        while not self._q.empty() and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)

    def status(self) -> dict:
        out = {"enabled": self.enabled, "path": self.path, "written": self.written,
               "dropped": self.dropped, "write_failed": self.failed}
        if self.reason:
            out["reason"] = self.reason
        return out


def demand_log_from_env(name: str, log: Callable[[str], None] = lambda *_: None) -> DemandLog:
    """`HARMONY_DEMAND_LOG_AGE_DAYS` is required (the design's value is 21:
    three weekly cycles for same-hour-last-week comparison). Unset disables."""
    age = os.environ.get("HARMONY_DEMAND_LOG_AGE_DAYS", "").strip()
    root = os.environ.get("HARMONY_DEMAND_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "livestack", "demand")
    return DemandLog(
        os.path.join(root, f"{name}.jsonl"),
        max_age_s=float(age) * 86400 if age else None,
        max_bytes=int(float(os.environ.get("HARMONY_DEMAND_LOG_MAX_MB", "64")) * 1024 * 1024),
        max_files=int(os.environ.get("HARMONY_DEMAND_LOG_FILES", "8")),
        log=log,
    )


def read_demand(path: str, since: float, limit: int = 50_000) -> list:
    """Records at or after `since` from the log and its rotations, oldest
    first, at most `limit` (the most recent win). Bounded so a caller asking
    for three weeks of a busy unit gets a truncated trace it can see is
    truncated (`len == limit`), not an unbounded read."""
    files = [path] + [f"{path}.{i}" for i in range(1, 64)]
    rows = []
    for p in reversed(files):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if float(r.get("ts") or 0) >= since:
                        rows.append(r)
        except FileNotFoundError:
            continue
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows[-limit:]


class UnitCostStore:
    """The last measured cost per composition hash, persisted so a proposal can
    be scored against compositions that are not running right now.

    Bounded: at most `max_rows` hashes, oldest measurement dropped. The file is
    rewritten whole on each put, which is fine at one write per engine start."""

    def __init__(self, path: str, max_rows: int = 256) -> None:
        self.path = path
        self.max_rows = max_rows
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def load(self) -> dict:
        rows = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("composition_hash"):
                        rows[row["composition_hash"]] = row
        except FileNotFoundError:
            pass
        return rows

    def put(self, row: dict) -> None:
        rows = self.load()
        rows[row["composition_hash"]] = row
        keep = sorted(rows.values(), key=lambda r: r.get("measured_at", 0))[-self.max_rows:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in keep:
                fh.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
        os.replace(tmp, self.path)
