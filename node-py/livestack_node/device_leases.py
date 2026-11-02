"""Durable broker-owned exclusive-device leases."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from numbers import Real
from typing import Any, Dict, Iterator, List, Optional


ACTIVE_STATES = ("draining", "exclusive", "expired_draining")
TERMINAL_STATES = ("released", "failed", "timeout")
MAX_WAIT_TIMEOUT_S = 86400
MAX_TTL_S = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_leases (
    request_id TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('draining', 'exclusive', 'expired_draining',
                  'released', 'failed', 'timeout')
    ),
    min_free_bytes INTEGER NOT NULL,
    free_bytes INTEGER,
    wait_timeout_s REAL NOT NULL,
    ttl_s REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    reason TEXT,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    boot_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS device_leases_one_active
    ON device_leases(device_id)
    WHERE state IN ('draining', 'exclusive', 'expired_draining');
CREATE INDEX IF NOT EXISTS device_leases_active
    ON device_leases(state, device_id);
"""


BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def current_boot_id() -> str:
    """This kernel boot's id, or '' where the kernel does not expose one."""
    try:
        with open(BOOT_ID_PATH) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _free_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("free_bytes must be a non-negative integer")
    return value


def _seconds(value: Any, name: str, maximum: int) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return result


def _now(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("now must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("now must be finite")
    return result


def _blockers(value: Any) -> str:
    if not isinstance(value, (list, dict)):
        raise ValueError("blockers must be a list or object")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("blockers must contain finite JSON values") from exc


def _record(row: sqlite3.Row) -> Dict[str, Any]:
    result = dict(row)
    result["blockers"] = json.loads(result.pop("blockers_json"))
    return result


class DeviceLeaseStore:
    """SQLite-backed device exclusivity with durable requests and leases."""

    def __init__(self, path: str, boot_id: Optional[str] = None):
        # Callers pass time.monotonic, which restarts near 0 at reboot, so a
        # lease persisted by an earlier boot carries an expires_at far in this
        # boot's future and would never expire. Its holder died with that boot,
        # so every lease stamped with another boot id is released on open.
        self._boot_id = current_boot_id() if boot_id is None else boot_id
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30,
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(device_leases)")}
            if "boot_id" not in columns:
                # Rows written before boot ids were recorded get '' (an unknown boot).
                self._db.execute(
                    "ALTER TABLE device_leases ADD COLUMN boot_id TEXT NOT NULL DEFAULT ''")
            if self._boot_id:
                self._db.execute(
                    f"""
                    UPDATE device_leases
                    SET state = 'released', reason = 'previous_boot'
                    WHERE boot_id != ? AND state IN ({','.join('?' for _ in ACTIVE_STATES)})
                    """,
                    [self._boot_id, *ACTIVE_STATES],
                )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _get(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT * FROM device_leases WHERE request_id = ?", (request_id,)
        ).fetchone()
        return _record(row) if row is not None else None

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        request_id = _identifier(request_id, "request_id")
        with self._lock:
            return self._get(request_id)

    def get_lease(self, lease_id: str) -> Optional[Dict[str, Any]]:
        lease_id = _identifier(lease_id, "lease_id")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM device_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            return _record(row) if row is not None else None

    def active(self, device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if device_id is not None:
            device_id = _identifier(device_id, "device_id")
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        sql = f"SELECT * FROM device_leases WHERE state IN ({placeholders})"
        params: List[Any] = list(ACTIVE_STATES)
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY created_at, request_id"
        with self._lock:
            return [_record(row) for row in self._db.execute(sql, params).fetchall()]

    def begin(
        self,
        request_id: str,
        device_id: str,
        owner: str,
        min_free_bytes: int,
        wait_timeout_s: float,
        ttl_s: float,
        now: float,
    ) -> Dict[str, Any]:
        request_id = _identifier(request_id, "request_id")
        device_id = _identifier(device_id, "device_id")
        owner = _identifier(owner, "owner")
        min_free_bytes = _positive_int(min_free_bytes, "min_free_bytes")
        wait_timeout_s = _seconds(wait_timeout_s, "wait_timeout_s", MAX_WAIT_TIMEOUT_S)
        ttl_s = _seconds(ttl_s, "ttl_s", MAX_TTL_S)
        now = _now(now)
        with self._transaction():
            existing = self._get(request_id)
            if existing is not None:
                if existing["owner"] != owner:
                    raise ValueError("request_id already belongs to another owner")
                expected = {
                    "device_id": device_id,
                    "min_free_bytes": min_free_bytes,
                    "wait_timeout_s": wait_timeout_s,
                    "ttl_s": ttl_s,
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise ValueError("request parameters do not match the existing request")
                return existing
            row = self._db.execute(
                f"SELECT request_id FROM device_leases "
                f"WHERE device_id = ? AND state IN ({','.join('?' for _ in ACTIVE_STATES)})",
                [device_id, *ACTIVE_STATES],
            ).fetchone()
            if row is not None:
                raise RuntimeError("device already has an active request")
            lease_id = f"device-lease-{uuid.uuid4().hex}"
            try:
                self._db.execute(
                    """
                    INSERT INTO device_leases (
                        request_id, lease_id, device_id, owner, state,
                        min_free_bytes, free_bytes, wait_timeout_s, ttl_s,
                        created_at, updated_at, expires_at, reason, blockers_json, boot_id
                    ) VALUES (?, ?, ?, ?, 'draining', ?, NULL, ?, ?, ?, ?, ?, NULL, '[]', ?)
                    """,
                    (
                        request_id,
                        lease_id,
                        device_id,
                        owner,
                        min_free_bytes,
                        wait_timeout_s,
                        ttl_s,
                        now,
                        now,
                        now + wait_timeout_s,
                        self._boot_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("device already has an active request") from exc
            result = self._get(request_id)
            assert result is not None
            return result

    def mark_exclusive(
        self,
        request_id: str,
        lease_id: str,
        free_bytes: int,
        now: float,
    ) -> Dict[str, Any]:
        request_id = _identifier(request_id, "request_id")
        lease_id = _identifier(lease_id, "lease_id")
        free_bytes = _free_bytes(free_bytes)
        now = _now(now)
        with self._transaction():
            existing = self._get(request_id)
            if existing is None:
                raise ValueError("unknown request_id")
            if existing["lease_id"] != lease_id:
                raise ValueError("lease_id does not match request_id")
            if existing["state"] == "exclusive":
                return existing
            if existing["state"] != "draining":
                raise RuntimeError("only a draining request can become exclusive")
            if existing["expires_at"] <= now:
                raise RuntimeError("draining request has expired")
            self._db.execute(
                """
                UPDATE device_leases
                SET state = 'exclusive', free_bytes = ?, updated_at = ?, expires_at = ?
                WHERE request_id = ? AND lease_id = ? AND state = 'draining'
                """,
                (free_bytes, now, now + existing["ttl_s"], request_id, lease_id),
            )
            result = self._get(request_id)
            assert result is not None
            return result

    def heartbeat(self, lease_id: str, ttl_s: float, now: float) -> bool:
        lease_id = _identifier(lease_id, "lease_id")
        ttl_s = _seconds(ttl_s, "ttl_s", MAX_TTL_S)
        now = _now(now)
        with self._transaction():
            cursor = self._db.execute(
                """
                UPDATE device_leases
                SET updated_at = ?, expires_at = ?
                WHERE lease_id = ? AND state = 'exclusive' AND expires_at > ?
                """,
                (now, now + ttl_s, lease_id, now),
            )
            return cursor.rowcount == 1

    def release(self, lease_id: str, now: float) -> bool:
        lease_id = _identifier(lease_id, "lease_id")
        now = _now(now)
        with self._transaction():
            row = self._db.execute(
                "SELECT state FROM device_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                return False
            if row["state"] == "released":
                return True
            if row["state"] not in ACTIVE_STATES:
                return False
            cursor = self._db.execute(
                f"""
                UPDATE device_leases
                SET state = 'released', updated_at = ?
                WHERE lease_id = ? AND state IN ({','.join('?' for _ in ACTIVE_STATES)})
                """,
                [now, lease_id, *ACTIVE_STATES],
            )
            return cursor.rowcount == 1

    def expire(self, now: float) -> List[Dict[str, Any]]:
        now = _now(now)
        with self._transaction():
            rows = self._db.execute(
                """
                SELECT * FROM device_leases
                WHERE state IN ('draining', 'exclusive') AND expires_at <= ?
                ORDER BY created_at, request_id
                """,
                (now,),
            ).fetchall()
            self._db.execute(
                """
                UPDATE device_leases
                SET state = 'expired_draining', updated_at = ?
                WHERE state IN ('draining', 'exclusive') AND expires_at <= ?
                """,
                (now, now),
            )
            expired = []
            for row in rows:
                result = _record(row)
                result["state"] = "expired_draining"
                result["updated_at"] = now
                expired.append(result)
            return expired

    def fail(
        self,
        request_id: str,
        reason: str,
        now: float,
        terminal_state: str = "failed",
    ) -> Dict[str, Any]:
        request_id = _identifier(request_id, "request_id")
        reason = _identifier(reason, "reason")
        now = _now(now)
        if terminal_state not in ("failed", "timeout"):
            raise ValueError("terminal_state must be 'failed' or 'timeout'")
        with self._transaction():
            existing = self._get(request_id)
            if existing is None:
                raise ValueError("unknown request_id")
            if existing["state"] not in ACTIVE_STATES:
                raise RuntimeError("request is already terminal")
            self._db.execute(
                """
                UPDATE device_leases
                SET state = ?, reason = ?, updated_at = ?
                WHERE request_id = ? AND state IN (?, ?, ?)
                """,
                (terminal_state, reason, now, request_id, *ACTIVE_STATES),
            )
            result = self._get(request_id)
            assert result is not None
            return result

    def blockers(
        self,
        request_id: str,
        blockers: Any,
        now: float,
    ) -> Dict[str, Any]:
        request_id = _identifier(request_id, "request_id")
        encoded = _blockers(blockers)
        now = _now(now)
        with self._transaction():
            existing = self._get(request_id)
            if existing is None:
                raise ValueError("unknown request_id")
            self._db.execute(
                "UPDATE device_leases SET blockers_json = ?, updated_at = ? WHERE request_id = ?",
                (encoded, now, request_id),
            )
            result = self._get(request_id)
            assert result is not None
            return result

    def state(self, device_id: str) -> Dict[str, Any]:
        device_id = _identifier(device_id, "device_id")
        with self._lock:
            row = self._db.execute(
                f"""
                SELECT state, owner, lease_id, blockers_json FROM device_leases
                WHERE device_id = ? AND state IN ({','.join('?' for _ in ACTIVE_STATES)})
                """,
                [device_id, *ACTIVE_STATES],
            ).fetchone()
            if row is None:
                return {"state": "shared", "owner": None, "lease_id": None, "blockers": []}
            return {
                "state": row["state"],
                "owner": row["owner"],
                "lease_id": row["lease_id"],
                "blockers": json.loads(row["blockers_json"]),
            }
