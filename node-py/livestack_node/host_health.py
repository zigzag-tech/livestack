"""This host's durable health gate, as Harmony model placement reads it.

A file, not a flag in memory: on zz-leaky (2026-11-14..) the RAM is under diagnosis, the owner
swaps DIMMs and reboots, and every boot changes the RAM hostd measures. A gate that a hostd
restart, a reboot or a capacity change could clear would load models into memory that silently
flips bits. The file is the operator's (or memory-test agent's) statement; nothing in Harmony
writes it, and measured capacity never reads as health.

    /etc/harmony/host-health.json   (override: LIVESTACK_HOST_HEALTH_FILE)
    {"state": "held", "owner": "memory-test", "reason": "bit-60 flips; verdict open"}

- Absent: eligible (every host that never needed one).
- `"state": "eligible"`: eligible (a released hold keeps its record).
- Anything else, unreadable or malformed: HELD. A gate that fails open is not a gate.

Read on every planning pass, cached on (mtime, size), so a change applies without a restart.
Stdlib only: the process-node facade carries its own copy of `read_file` semantics.
"""
from __future__ import annotations

import json
import os
from typing import Optional

ENV = "LIVESTACK_HOST_HEALTH_FILE"
DEFAULT_PATH = "/etc/harmony/host-health.json"
MAX_BYTES = 4096


def default_path() -> str:
    return os.environ.get(ENV) or DEFAULT_PATH


def _held(reason: str, path: str, owner: Optional[str] = None) -> dict:
    return {"state": "held", "held": True, "owner": owner, "reason": reason[:240], "source": path}


def read_file(path: str) -> dict:
    """The gate as the file states it; see the module docstring for the fail-closed rules."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {"state": "eligible", "held": False, "owner": None, "reason": None, "source": None}
    except OSError as e:
        return _held(f"host health file unreadable: {type(e).__name__}", path)
    if len(raw) > MAX_BYTES:
        return _held("host health file exceeds 4 KiB", path)
    try:
        doc = json.loads(raw)
    except ValueError:
        return _held("host health file is not JSON", path)
    if not isinstance(doc, dict) or doc.get("state") not in ("held", "eligible"):
        return _held("host health file has no state 'held' or 'eligible'", path)
    owner = doc.get("owner") if isinstance(doc.get("owner"), str) else None
    reason = doc.get("reason") if isinstance(doc.get("reason"), str) else ""
    if doc["state"] == "held":
        return _held(reason or "held (no reason given)", path, owner)
    return {"state": "eligible", "held": False, "owner": owner, "reason": reason[:240] or None, "source": path}


class HostHealthFile:
    """`read_file` with a stat cache: hostd consults it on every plan."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        self._key = None
        self._value = None

    def read(self) -> dict:
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            key = None
        except OSError:
            return read_file(self.path)
        if self._value is None or key != self._key:
            self._key, self._value = key, read_file(self.path)
        return dict(self._value)
