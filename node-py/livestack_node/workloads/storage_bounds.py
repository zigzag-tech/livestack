"""Effective byte bounds derived from filesystem headroom (openspec/changes/storage-headroom-admission).

One place turns configuration plus a `statvfs` reading into: the effective byte cap of a
store (`min(absolute cap, capacity_fraction x filesystem size)`), the free-space floor
(`max(headroom_bytes, headroom_fraction x filesystem size)`), and the state (`ok`, `low`,
`refusing`, `unknown`). Absent configuration reproduces today's behaviour: only the absolute
cap applies. Plain Python validation (no values are echoed in errors).
"""
import logging
import math
import os
import threading
import time
from dataclasses import dataclass

MIB = 1024**2
KNOWN_ROOT = ('objects', 'refresh_seconds', 'unknown_allowance_bytes', 'gc_batch')
KNOWN_STORE = ('capacity_fraction', 'headroom_bytes', 'headroom_fraction', 'alert_fraction')
EVENTS_KEPT = 8


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class StoreBound:
    capacity_fraction: float | None = None
    headroom_bytes: int = 0
    headroom_fraction: float = 0.0
    alert_fraction: float | None = None


@dataclass(frozen=True)
class StorageBounds:
    objects: StoreBound = StoreBound()
    refresh_seconds: float = 15
    unknown_allowance_bytes: int = MIB
    gc_batch: int = 256

    @classmethod
    def validate(cls, raw):
        """Return StorageBounds or raise ValueError('<key>: <message>; ...') without values."""
        if not isinstance(raw, dict):
            raise ValueError('storage_bounds: Input should be a valid dictionary')
        problems = [f'storage_bounds.{k}: Extra inputs are not permitted' for k in raw if k not in KNOWN_ROOT]
        values, store = {}, {}
        for key, low in (('refresh_seconds', 5), ('unknown_allowance_bytes', 0), ('gc_batch', 1)):
            if key in raw:
                if not _num(raw[key]) or raw[key] < low:
                    problems.append(f'storage_bounds.{key}: Input should be a number >= {low}')
                else:
                    values[key] = raw[key] if key == 'refresh_seconds' else int(raw[key])
        section = raw.get('objects', {})
        if not isinstance(section, dict):
            problems.append('storage_bounds.objects: Input should be a valid dictionary')
            section = {}
        problems += [f'storage_bounds.objects.{k}: Extra inputs are not permitted' for k in section if k not in KNOWN_STORE]
        for key in ('capacity_fraction', 'headroom_fraction', 'alert_fraction'):
            if key in section:
                if not _num(section[key]) or not 0 < section[key] <= 1:
                    problems.append(f'storage_bounds.objects.{key}: Input should be in (0, 1]')
                else:
                    store[key] = float(section[key])
        if 'headroom_bytes' in section:
            if not _num(section['headroom_bytes']) or section['headroom_bytes'] <= 0:
                problems.append('storage_bounds.objects.headroom_bytes: Input should be greater than 0')
            else:
                store['headroom_bytes'] = int(section['headroom_bytes'])
        if 'alert_fraction' in store and store['alert_fraction'] < store.get('headroom_fraction', 0.0):
            problems.append('storage_bounds.objects.alert_fraction: must be >= headroom_fraction')
        if problems:
            raise ValueError('; '.join(problems))
        return cls(objects=StoreBound(**store), **values)


class HeadroomGuard:
    """Reads a filesystem and answers admission questions for one store.

    `statvfs` is a test seam. A read failure is never treated as "plenty of room": `state`
    is `unknown` and `check` refuses above the small allowance."""

    def __init__(self, path, absolute_cap, bounds, *, statvfs=os.statvfs, clock=time.monotonic, name='objects'):
        self.path, self.absolute_cap, self.bounds = path, absolute_cap, bounds
        self.statvfs, self.clock, self.name = statvfs, clock, name
        self._lock = threading.Lock()
        self._last = None          # (effective, state) last logged
        self.events = []           # bounded: state/effective changes

    def _read(self):
        try:
            stats = self.statvfs(str(self.path))
            return stats.f_blocks*stats.f_frsize, stats.f_bavail*stats.f_frsize
        except (OSError, ValueError, AttributeError):
            return None

    def snapshot(self, log=True):
        """Fresh reading: the inputs, the effective bound, the floor and the state."""
        b = self.bounds.objects
        read = self._read()
        if read is None:
            snap = dict(filesystem=str(self.path), state='unknown', effective_max_bytes=self.absolute_cap,
                        absolute_cap=self.absolute_cap, floor_bytes=None, free_bytes=None, fs_total_bytes=None,
                        free_fraction=None)
        else:
            total, free = read
            effective = self.absolute_cap
            if b.capacity_fraction is not None:
                effective = min(effective, int(b.capacity_fraction*total))
            floor = max(b.headroom_bytes, math.ceil(b.headroom_fraction*total))
            alert = (b.alert_fraction*total) if b.alert_fraction is not None else floor*1.5
            configured = b.headroom_bytes > 0 or b.headroom_fraction > 0
            state = 'ok'
            if configured and free < floor:
                state = 'refusing'
            elif configured and free < alert:
                state = 'low'
            snap = dict(filesystem=str(self.path), state=state, effective_max_bytes=effective,
                        absolute_cap=self.absolute_cap, capacity_fraction=b.capacity_fraction,
                        floor_bytes=floor if configured else 0, free_bytes=free, fs_total_bytes=total,
                        free_fraction=round(free/total, 4) if total else None)
        if log:
            self._record(snap)
        return snap

    def _record(self, snap):
        with self._lock:
            key = (snap['state'], snap['effective_max_bytes'])
            last = self._last
            changed = (last is None or last[0] != key[0] or
                       abs(key[1]-last[1]) > 0.01*max(1, last[1]))
            if not changed:
                return
            self._last = key
            line = ('storage bound %s: effective=%s (cap=%s, fraction=%s) floor=%s free=%s fs_total=%s state=%s'
                    % (self.name, snap['effective_max_bytes'], snap['absolute_cap'], snap.get('capacity_fraction'),
                       snap['floor_bytes'], snap['free_bytes'], snap['fs_total_bytes'], snap['state']))
            (logging.info if snap['state'] in ('ok', 'low') else logging.warning)(line)
            self.events = (self.events + [dict(at=time.time(), state=snap['state'],
                                               effective_max_bytes=snap['effective_max_bytes'],
                                               free_bytes=snap['free_bytes'])])[-EVENTS_KEPT:]

    def deficit(self, size, pending=0, snap=None):
        """Bytes that must be freed before `size` more may be admitted; 0 when admitted;
        None when free space cannot be read and the put is above the allowance."""
        snap = snap or self.snapshot()
        if snap['state'] == 'unknown':
            return None if size > self.bounds.unknown_allowance_bytes else 0
        return max(0, snap['floor_bytes']-(snap['free_bytes']-pending-size))
