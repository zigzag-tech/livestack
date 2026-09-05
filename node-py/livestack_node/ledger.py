"""ledger.py — the decision ledger: every placement and routing decision leaves
enough behind to be second-guessed.

The requirement, in the operator's words: *most requests should have sufficient
logs for a future agent to look at and say: this task was routed to this machine,
and because it is remote, or less capable, or busier, it should have been routed
to that machine which sat idle at the time.*

A record is SUFFICIENT when a reader who was not present can answer all five of
these from the record alone, without a live system:

1. what was asked, 2. what was known — the VALUES the decision used, not a
pointer to a view that has since changed, 3. what was chosen and why, 4. **why
each loser lost**, 5. what happened.

A record lacking (4) or (5) is a log line, not a decision record. Today's
``[hostbroker] evict llm@…: relieve measured over-budget pressure`` has (3) and
nothing else — it says what, not what else was possible.

Design notes that are load-bearing rather than decorative:

* **Values, not references.** `Candidate` carries numbers, and `inputs_at`
  carries when they were measured, because a rank built on a 40 s old load
  reading is a different decision from one built on a 2 s old reading and the
  retrospective needs to tell them apart.
* **Every candidate, including the ones filtered out first.** "Filtered" is a
  `reason`, not an omission. The absent row is exactly what stops a reader from
  saying "it should have gone to X".
* **Bounded before the first record is written** (benchday rule 10). See
  :class:`JsonlLedger`: the bound is size x files plus an age window, the
  enforcer is the writer itself, and an unset age window means DISABLED — never
  "delete on the next deploy".
* **No secrets, no audio, no transcripts.** `owner` is an id. This is for
  engineers auditing routing; it is not the flight recorder.

See ``_plans/decision-ledger.md``. The schema is ``decision.schema.json``, next
to this file, and is the single definition every emitter validates against.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "decision.schema.json")

# Bounds. A per-record cap keeps one pathological decision from wedging a reader;
# a candidate cap keeps a 500-node fleet from writing a 500-row row.
MAX_RECORD_BYTES = 32 * 1024
MAX_CANDIDATES = 64

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_ulid_lock = threading.Lock()
_ulid_last = (0, 0)          # (millisecond, random component)


def new_decision_id(now: Optional[float] = None,
                    rand: Optional[Callable[[int], int]] = None) -> str:
    """A **monotonic** ULID: 48-bit millisecond timestamp then 80 bits, Crockford
    base32, 26 chars.

    Sortable and unique, which is what the correlation story needs — the id is
    carried with the request across processes and the outcome is joined back to
    it later, so it has to be comparable without a lookup. Hand-rolled rather
    than a dependency: `livestack_node` is deliberately pure-stdlib so that
    provisioning and leasing import on a machine with no compiled core.

    Monotonic matters and a plain ULID is not. Within one millisecond the random
    component decides the order, so two records written in the same millisecond
    sort ARBITRARILY — which shows up the moment anything replays a ledger and
    expects the order it was written in. Inside a millisecond the previous
    random component is incremented instead of redrawn; a fresh millisecond
    draws again.
    """
    ms = int((time.time() if now is None else now) * 1000)
    draw = rand or (lambda n: random.getrandbits(n))
    global _ulid_last
    with _ulid_lock:
        last_ms, last_rnd = _ulid_last
        if ms == last_ms:
            rnd = last_rnd + 1
            if rnd >= 1 << 80:          # 2^80 ids in one ms is not a real case,
                ms += 1                 # but rolling into the next ms keeps the
                rnd = draw(80)          # ordering guarantee unconditional.
        elif ms < last_ms:
            # A clock that went backwards must not produce ids that sort before
            # ones already written. Time is for reading; ordering is the promise.
            ms, rnd = last_ms, last_rnd + 1
        else:
            rnd = draw(80)
        _ulid_last = (ms, rnd)
    value = (ms << 80) | rnd
    out = []
    for i in range(25, -1, -1):
        out.append(_CROCKFORD[(value >> (i * 5)) & 0x1F])
    return "".join(out)


def distance_band(distance_ms: Optional[float]) -> str:
    """Bucket a measured distance.

    Bands, not raw milliseconds: a client picker re-measures the fine detail
    itself, so the fleet's job is only to keep a request from STARTING on the
    wrong continent. Bucketing also keeps ranking stable under jitter, which
    raw millis are not.
    """
    if distance_ms is None:
        return "unknown"
    if distance_ms < 50:
        return "<50"
    if distance_ms < 200:
        return "<200"
    if distance_ms < 600:
        return "<600"
    return ">=600"


@dataclass
class Candidate:
    """One candidate considered, with the inputs as they were at that instant.

    `outcome` is `chosen` | `ranked` | `filtered`, and `reason` is never empty:
    it is the term that placed this candidate or the first rule that eliminated
    it. That field is what lets a reader say "it should have gone to X" — if X's
    row reads `filtered: state=suspect` the decision was right given what was
    known, and if it reads `ranked lower: load 0.2 vs 0.0` the reader can go
    check whether load was measured correctly.
    """
    id: str
    outcome: str
    reason: str
    host_id: Optional[str] = None
    device_id: Optional[str] = None
    state: Optional[str] = None
    ready: Optional[bool] = None
    distance_ms: Optional[float] = None
    distance_band: Optional[str] = None
    load: Optional[Dict[str, Any]] = None
    resident: Optional[bool] = None
    region: Optional[str] = None
    inputs_at: Optional[float] = None
    rank: Optional[int] = None

    def __post_init__(self):
        if self.distance_band is None:
            self.distance_band = distance_band(self.distance_ms)


@dataclass
class Decision:
    emitter: str
    emitter_id: str
    decision: str
    candidates: List[Candidate] = field(default_factory=list)
    kind: Optional[str] = None
    request: Optional[Dict[str, Any]] = None
    chosen: Optional[str] = None
    reason: Optional[str] = None
    ttl_s: Optional[float] = None
    parent_decision_id: Optional[str] = None
    outcome: Optional[Dict[str, Any]] = None
    decision_id: str = field(default_factory=new_decision_id)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Filtered rows first, then ranked/chosen in rank order. The reader's
        # first question is "what was even possible", and the eliminations are
        # the answer to it.
        order = {"filtered": 0, "chosen": 1, "ranked": 1}
        d["candidates"] = sorted(
            d["candidates"],
            key=lambda c: (order.get(c["outcome"], 2),
                           c["rank"] if c.get("rank") is not None else 1 << 30,
                           c["id"]),
        )
        if len(d["candidates"]) > MAX_CANDIDATES:
            d["truncated"] = len(d["candidates"]) - MAX_CANDIDATES
            d["candidates"] = d["candidates"][:MAX_CANDIDATES]
        return {k: v for k, v in d.items() if v is not None or k in ("chosen",)}


class JsonlLedger:
    """Append-only JSONL with rotation and an age window.

    **The bound and its enforcer, stated up front** (benchday rule 10): at most
    ``max_bytes`` per file and ``max_files`` files, plus an optional
    ``max_age_s`` after which a rotated file is deleted. The enforcer is this
    writer — a size check on every append, and an age sweep on rotation. The
    precedent this exists not to repeat is ``~/.benchday/llm-costs.jsonl``: an
    append-only JSONL with no rotation and no inventory row.

    ``max_age_s=None`` means the age window is DISABLED. A delete-shaped bound
    must never default to deleting: an unset window that meant "14 days" would
    silently destroy history on the deploy that introduced it.

    Rate is bounded by the CALLERS, not here — the host broker emits only when a
    plan has actions, the fleet broker only on membership transitions. That
    discipline is what keeps this from becoming the 92,089-line log membership
    was built to end.
    """

    def __init__(self, path: str, max_bytes: int = 32 * 1024 * 1024,
                 max_files: int = 4, max_age_s: Optional[float] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda *_: None):
        self.path = path
        self.max_bytes = max_bytes
        self.max_files = max(1, max_files)
        self.max_age_s = max_age_s
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    # -- writing -------------------------------------------------------------

    def append(self, decision: Decision) -> Optional[dict]:
        """Write one record. Returns what was written, or None if it could not
        be — a ledger that cannot write must never take the decision with it."""
        try:
            payload = self._encode(decision)
        except Exception as exc:      # pragma: no cover - a record that will not serialize
            self._log(f"[ledger] unserializable record dropped: {exc}")
            return None
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        try:
            with self._lock:
                self._rotate_if_needed(len(line) + 1)
                with open(self.path, "a", encoding="utf-8") as fh:
                    # A crash mid-write leaves a line with no terminator. Append
                    # one before ours, so the damage is the ONE truncated record
                    # rather than that record plus the next one glued to it.
                    if fh.tell() and not self._ends_with_newline():
                        fh.write("\n")
                    fh.write(line + "\n")
        except Exception as exc:
            # Emitting is observability. It is never worth failing a placement
            # or a route for, so a full disk degrades to silence, once.
            self._log(f"[ledger] write failed: {exc}")
            return None
        return payload

    def _encode(self, decision: Decision) -> dict:
        payload = decision.to_dict()
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(raw) <= MAX_RECORD_BYTES:
            return payload
        # Over the cap. Shed candidates from the BACK — the eliminated and
        # worst-ranked rows — because the winner and the near-misses are what a
        # retrospective actually reads.
        cands = payload.get("candidates", [])
        dropped = int(payload.get("truncated") or 0)
        while cands and len(raw) > MAX_RECORD_BYTES:
            cands.pop()
            dropped += 1
            payload["truncated"] = dropped
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return payload

    def _ends_with_newline(self) -> bool:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) == b"\n"
        except OSError:
            return True

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size + incoming <= self.max_bytes:
            return
        # path.(N-1) -> path.N, dropping whatever falls off the end.
        oldest = f"{self.path}.{self.max_files - 1}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(self.max_files - 2, 0, -1):
            src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(self.path, f"{self.path}.1")
        self._prune_old()

    def _prune_old(self) -> List[str]:
        """Delete rotated files past the age window. An unset window deletes
        nothing at all — see the class docstring."""
        if self.max_age_s is None:
            return []
        now = self._clock()
        gone = []
        for i in range(1, self.max_files):
            f = f"{self.path}.{i}"
            try:
                if os.path.exists(f) and now - os.path.getmtime(f) > self.max_age_s:
                    os.remove(f)
                    gone.append(f)
            except OSError:
                continue
        if gone:
            self._log(f"[ledger] pruned {len(gone)} file(s) past {self.max_age_s:.0f}s")
        return gone

    # -- reading -------------------------------------------------------------

    def read(self, since: Optional[float] = None,
             kind: Optional[str] = None,
             limit: int = 1000) -> List[dict]:
        """Records newest-file-last, oldest first, for the retrospective.

        A malformed line is SKIPPED rather than raising: a ledger truncated by a
        crash mid-write must still be readable, or the one record that explains
        the crash is the one nobody can get to.
        """
        out: List[dict] = []
        for f in self._files_oldest_first():
            for rec in self._read_file(f):
                if since is not None and rec.get("ts", 0) < since:
                    continue
                if kind is not None and rec.get("kind") != kind:
                    continue
                out.append(rec)
        return out[-limit:] if limit and len(out) > limit else out

    def _files_oldest_first(self) -> List[str]:
        files = [f"{self.path}.{i}" for i in range(self.max_files - 1, 0, -1)]
        files.append(self.path)
        return [f for f in files if os.path.exists(f)]

    @staticmethod
    def _read_file(path: str) -> Iterator[dict]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except OSError:
            return


def default_ledger_path(name: str) -> str:
    """``~/.cache/livestack/<name>.jsonl``, overridable by
    ``LIVESTACK_LEDGER_DIR``."""
    d = os.environ.get("LIVESTACK_LEDGER_DIR") or os.path.expanduser("~/.cache/livestack")
    return os.path.join(d, f"{name}.jsonl")


def ledger_from_env(name: str, default_max_mb: int,
                    log: Callable[[str], None] = lambda *_: None
                    ) -> Optional[JsonlLedger]:
    """Build a ledger from the environment, or None when disabled.

    ``LIVESTACK_LEDGER=0`` turns emission off entirely. ``LIVESTACK_LEDGER_AGE_DAYS``
    is UNSET by default and unset means the age window is disabled — the same
    rule the peer roster's prune window follows, for the same reason: this bound
    deletes rather than rotates.
    """
    if os.environ.get("LIVESTACK_LEDGER", "1") == "0":
        return None
    age = os.environ.get("LIVESTACK_LEDGER_AGE_DAYS", "").strip()
    mb = float(os.environ.get("LIVESTACK_LEDGER_MAX_MB", str(default_max_mb)))
    return JsonlLedger(
        default_ledger_path(name),
        max_bytes=int(mb * 1024 * 1024),
        max_files=int(os.environ.get("LIVESTACK_LEDGER_FILES", "4")),
        max_age_s=float(age) * 86400 if age else None,
        log=log,
    )


def validate(record: dict) -> List[str]:
    """Check a record against ``decision.schema.json``. Returns the problems, so
    a caller can assert emptiness; an empty list means valid.

    Uses `jsonschema` when it is installed and otherwise falls back to the
    required-field and enum checks that matter most — the package is
    pure-stdlib on purpose (see ``__init__.py``), so validation must not become
    the thing that makes it importable only with a dependency.
    """
    try:
        import jsonschema  # type: ignore
        with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
            schema = json.load(fh)
        v = jsonschema.Draft7Validator(schema)
        return [f"{'/'.join(str(p) for p in e.path)}: {e.message}"
                for e in sorted(v.iter_errors(record), key=lambda e: list(e.path))]
    except ImportError:
        return _validate_minimal(record)


def _validate_minimal(record: dict) -> List[str]:
    problems: List[str] = []
    for f in ("decision_id", "ts", "emitter", "emitter_id", "decision", "candidates"):
        if f not in record:
            problems.append(f"{f}: required")
    if len(str(record.get("decision_id", ""))) != 26:
        problems.append("decision_id: must be a 26-char ULID")
    if record.get("emitter") not in ("host-broker", "fleet-broker", "client-picker",
                                     "hub-manifest", "job-caller", None):
        problems.append(f"emitter: {record.get('emitter')!r} is not a known emitter")
    for i, c in enumerate(record.get("candidates") or []):
        if not c.get("id"):
            problems.append(f"candidates/{i}/id: required")
        if c.get("outcome") not in ("chosen", "ranked", "filtered"):
            problems.append(f"candidates/{i}/outcome: {c.get('outcome')!r} is not valid")
        if not c.get("reason"):
            problems.append(f"candidates/{i}/reason: required and must be non-empty")
    return problems
