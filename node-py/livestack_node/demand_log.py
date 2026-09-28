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
from typing import Callable, Optional

from .ledger import JsonlLedger

_TAIL_BYTES = 16 * 1024
_PROMPT = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_COMPLETION = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')


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

    def feed(self, chunk: bytes) -> None:
        self._buf = (self._buf + chunk)[-self._limit:]

    def tokens(self):
        def last(pat):
            found = pat.findall(self._buf)
            return int(found[-1]) if found else None
        return last(_PROMPT), last(_COMPLETION)


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
