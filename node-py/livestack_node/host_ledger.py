"""One host-RAM ledger per machine, read and written by every Harmony planner on it.

Two planners place work on one machine's RAM: the host broker (`hostbroker`,
model servers — a Strata unit declares `ram_gb` 45) and workload placement
(`workloads/placement.py`, task attempts — an e2e attempt may reach 10 GiB).
Before this each saw the other only by guessing:

* the broker's host pool was MemAvailable minus a reserve, so an attempt
  admitted at 10 GiB that had touched 2 GiB left 8 GiB that LOOKED free, and a
  45 GiB load could be granted into it;
* placement charged a model server its learned peak whenever its units were
  not resident, so a cold Strata held ~40 GB of a 60 GB host that nothing was
  using (zz-whitby 2026-10-18: every task worker there held idle).

Here each claimant WRITES what it holds or is about to take, and each planner
subtracts the other side's outstanding part from the same measurement:

    attempt  <attempt id>   a task attempt's memory claim (its cgroup cap);
                            the outstanding part is claim - cgroup current
    service  <cgroup path>  "this cgroup's host RAM is planned by the host
                            broker": workers stop charging its learned peak
    load     <unit kind>    a `ram_bytes` unit WAITING for host RAM or LOADING;
                            workers charge it in full, so new attempts drain

Policy (owner's decision, 2026-10-18): a model load DRAINS, it never kills. A
waiting load stops new attempts from being admitted on the host; running
attempts finish; the load then fits.

Entries are small JSON files, written atomically, each with an expiry its
writer refreshes; a writer that dies stops refreshing and its entries lapse, so
nothing has to clean up after a crash. Stdlib only: workload workers run on the
system python3. Unknown is never zero — an unreadable ledger is an empty one,
which is exactly the behaviour before it existed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

KINDS = ("attempt", "service", "load")
MAX_ENTRIES = 256
MAX_ENTRY_BYTES = 4096
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def default_dir() -> Path:
    """`LIVESTACK_HOST_LEDGER_DIR`, else `~/.local/state/livestack/host-ledger`.

    Every writer on a host must agree on it: the broker and harmony-llm run as
    system services with `User=ubuntu` and the workers as that user's services,
    so the user's state directory is the one place all of them share."""
    env = os.environ.get("LIVESTACK_HOST_LEDGER_DIR", "").strip()
    return Path(env) if env else Path.home() / ".local" / "state" / "livestack" / "host-ledger"


class HostLedger:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else default_dir()
        self.clock = clock

    def _file(self, kind: str, key: str) -> Path:
        if kind not in KINDS:
            raise ValueError(f"host ledger kind must be one of {KINDS}, not {kind!r}")
        return self.path / f"{kind}-{_SAFE.sub('_', key)[:160]}.json"

    def put(self, kind: str, key: str, ttl_s: float, **fields) -> None:
        """Write (or refresh) one entry. Never raises: the ledger is planning
        input, and a full disk must not fail the work it describes."""
        entry = {**fields, "kind": kind, "key": key, "written_at": self.clock(),
                 "expires_at": self.clock() + float(ttl_s)}
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            target = self._file(kind, key)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(entry, sort_keys=True))
            tmp.replace(target)
        except OSError:
            pass

    def drop(self, kind: str, key: str) -> None:
        try:
            self._file(kind, key).unlink()
        except OSError:
            pass

    def entries(self, kind: "str | None" = None) -> "list[dict]":
        """Live entries (unexpired), of one kind or all. Expired files are
        removed in passing; malformed ones are skipped, never trusted."""
        now = self.clock()
        out = []
        try:
            names = sorted(os.listdir(self.path))[:MAX_ENTRIES * 2]
        except OSError:
            return []
        for name in names:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            if kind is not None and not name.startswith(f"{kind}-"):
                continue
            path = self.path / name
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_ENTRY_BYTES:
                    continue
                entry = json.loads(raw)
                expires = float(entry["expires_at"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
                continue
            if expires <= now:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            out.append(entry)
            if len(out) >= MAX_ENTRIES:
                break
        return out

    # -- what each side reads ------------------------------------------------
    def attempt_outstanding_bytes(self, current_by_attempt: "dict[str, int]") -> int:
        """Σ over live attempt entries of max(0, claim - what its cgroup holds
        now): the part of every admitted attempt that MemAvailable does not show
        yet. `current_by_attempt` is `HostView`'s attempt scan (attempt id ->
        non-reclaimable bytes); an attempt with no cgroup yet (still preparing)
        is charged its whole claim."""
        total = 0
        for e in self.entries("attempt"):
            try:
                claim = int(e.get("claim_bytes") or 0)
            except (TypeError, ValueError):
                continue
            total += max(0, claim - int(current_by_attempt.get(e["key"], 0) or 0))
        return total

    def planned_services(self) -> "dict[str, list[str]]":
        """cgroup path -> unit kinds whose host RAM the host broker plans."""
        out = {}
        for e in self.entries("service"):
            units = e.get("units")
            out[e["key"]] = sorted(units) if isinstance(units, (list, dict)) else []
        return out

    def loads(self) -> "list[dict]":
        """`ram_bytes` units waiting for host RAM or loading: {unit, ram_bytes, state}."""
        out = []
        for e in self.entries("load"):
            try:
                out.append({"unit": e["key"], "ram_bytes": int(e.get("ram_bytes") or 0),
                            "state": str(e.get("state") or "waiting"),
                            "reason": str(e.get("reason") or "")[:200]})
            except (TypeError, ValueError):
                continue
        return out
