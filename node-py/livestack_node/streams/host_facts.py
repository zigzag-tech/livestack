"""`harmony.host/1`: the digest and modification time of the host's unit declarations.

design D3: an operator who hand-edits `/etc/harmony/llm-units.json` and restarts a unit is working
around Harmony. This contract makes that edit observable: the digest changes, `changed_ms` says when,
and a trigger rule can ask whether any `harmony.request/1` or reviewed deployment explains it.

Which files: hostd itself reads units from the LIVESTACK_UNITS env, but the unit DECLARATIONS the host's
nodes load are files (harmony-llm reads `HARMONY_LLM_UNITS_FILE`, `/etc/harmony/llm-units*.json`). The
patterns come from `HARMONY_UNITS_GLOBS` (os.pathsep-separated globs); the default is
`/etc/harmony/*units*.json`. Backups such as `llm-units.json.bak-2026...` do not end in `.json` and so
are not declarations.

Digest: SHA-256 over, for each matching file in sorted-path order, a framed record of its basename and
canonical bytes (JSON re-serialised with sorted keys when it parses, so a whitespace-only edit does not
read as drift; raw bytes otherwise). A file that matches but cannot be read is a FAILURE with its own
code (`units_unreadable`, naming the path), never a digest over fewer files (rule 13).

Storage bound (rule 10): ONE small state file (`host_facts.json`, at most 1 KiB: the last digest and
`changed_ms`), rewritten atomically in place; the enforcer is `_save` refusing to write more than
STATE_MAX_BYTES. Nothing grows with traffic or uptime.

Standard library only; relative imports only (the lane copies this directory).
"""
from __future__ import annotations

import glob as _glob
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from .common import bounded, canonical, optional, sha256_digest, Clock

DEFAULT_GLOBS = ("/etc/harmony/*units*.json",)
STATE_MAX_BYTES = 1024
MAX_FILES = 64
MAX_FILE_BYTES = 4 * 1024 * 1024
HEARTBEAT_S = 30.0


class UnitsUnreadable(Exception):
    """A declaration file matched but could not be read; `path` names it."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path, self.reason = path, reason


@dataclass(frozen=True)
class Scan:
    digest: str
    mtime_ms: int
    count: int


def globs_from_env(env: Optional[dict] = None) -> Sequence[str]:
    env = os.environ if env is None else env
    raw = env.get("HARMONY_UNITS_GLOBS", "").strip()
    return tuple(p for p in raw.split(os.pathsep) if p) if raw else DEFAULT_GLOBS


class UnitDeclarations:
    """The set of unit declaration files and how to digest them. Filesystem access is injectable."""

    def __init__(self, patterns: Sequence[str] = DEFAULT_GLOBS,
                 list_files: Callable[[str], List[str]] = _glob.glob,
                 read_bytes: Optional[Callable[[str], bytes]] = None,
                 mtime_ms: Optional[Callable[[str], int]] = None) -> None:
        self.patterns = tuple(patterns)
        self._list = list_files
        self._read = read_bytes or self._read_file
        self._mtime = mtime_ms or (lambda path: int(os.stat(path).st_mtime * 1000))

    @staticmethod
    def _read_file(path: str) -> bytes:
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise UnitsUnreadable(path, "larger than the 4 MiB declaration bound")
        return data

    @property
    def path_summary(self) -> str:
        return bounded(",".join(self.patterns), 256)

    def scan(self) -> Scan:
        paths = sorted({p for pattern in self.patterns for p in self._list(pattern)})
        if len(paths) > MAX_FILES:
            raise UnitsUnreadable(self.path_summary, f"{len(paths)} files exceed the {MAX_FILES}-file bound")
        framed = bytearray()
        newest = 0
        for path in paths:
            try:
                data = self._read(path)
                newest = max(newest, self._mtime(path))
            except UnitsUnreadable:
                raise
            except OSError as error:
                raise UnitsUnreadable(path, error.strerror or type(error).__name__) from error
            try:
                data = canonical(json.loads(data))
            except ValueError:
                pass  # not JSON: its raw bytes are its canonical form
            name = os.path.basename(path).encode("utf-8")
            framed += b"%d:%s\0%d:" % (len(name), name, len(data)) + data + b"\n"
        return Scan(sha256_digest(bytes(framed)), newest, len(paths))


PublishFn = Callable[..., Awaitable[Any]]


class HostFacts:
    """Decides when `harmony.host/1` is published: on a digest change, and on a heartbeat."""

    def __init__(self, host: str, declarations: UnitDeclarations, state_path: str, publish: PublishFn,
                 clock: Optional[Clock] = None, heartbeat_s: float = HEARTBEAT_S,
                 log: Callable[[str], None] = lambda _m: None) -> None:
        self.host = bounded(host, 128)
        self.declarations = declarations
        self.state_path = state_path
        self.publish = publish
        self.clock = optional(clock)
        self.heartbeat_ms = int(heartbeat_s * 1000)
        self.log = log
        self.digest: Optional[str] = None
        self.changed_ms = 0
        self.last_published_ms = -10 ** 15
        self._failing = False
        self._load()

    def _load(self) -> None:
        try:
            with open(self.state_path, "rb") as handle:
                state = json.loads(handle.read(STATE_MAX_BYTES + 1))
            digest, changed = state.get("digest"), state.get("changed_ms")
            if isinstance(digest, str) and isinstance(changed, int) and changed >= 0:
                self.digest, self.changed_ms = digest, changed
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            # A corrupt state file loses only `changed_ms` history; say so rather than start silently.
            self.log(f"host_facts: state unreadable ({error}); changed_ms restarts from the next observation")

    def _save(self) -> None:
        data = json.dumps({"digest": self.digest, "changed_ms": self.changed_ms}).encode()
        if len(data) > STATE_MAX_BYTES:
            raise ValueError("host_facts state exceeds its bound")
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, self.state_path)

    def body(self, scan: Scan) -> dict:
        return {"host": self.host, "units_digest": scan.digest, "units_mtime_ms": scan.mtime_ms,
                "units_count": scan.count, "units_path": self.declarations.path_summary,
                "changed_ms": self.changed_ms}

    async def tick(self) -> Optional[dict]:
        """One observation. Returns the body published, or None when nothing was due."""
        now = self.clock()
        try:
            scan = self.declarations.scan()
        except UnitsUnreadable as error:
            self._failing = True
            self.log(f"host_facts: units_unreadable {error}")
            await self.publish("failed", None, {"code": "units_unreadable", "detail": bounded(str(error), 240)})
            return None
        if scan.digest != self.digest:
            first = self.digest is None
            self.digest = scan.digest
            # With no earlier observation the best statement of "when it changed" is the files' own mtime.
            self.changed_ms = min(scan.mtime_ms, now) if first and scan.mtime_ms else now
            self._save()
            self.log(f"host_facts: units digest {'first seen' if first else 'CHANGED'} {scan.digest[:19]} "
                     f"({scan.count} files)")
        elif not self._failing and now - self.last_published_ms < self.heartbeat_ms:
            return None
        self._failing = False
        body = self.body(scan)
        await self.publish("ok", body, None)
        self.last_published_ms = now
        return body
