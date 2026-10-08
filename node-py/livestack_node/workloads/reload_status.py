"""Whether authority.json edits are applied: the answer to "did my edit take?".

The authority never watches its config file. SIGHUP (service.reload_principals) is the only trigger, and
until 2026-10-08 nothing said whether an edit had been read. `GET reload/status` shows the hash of the
file as last applied, the hash of the file now, and the last refusal, so "edited but not signalled" and
"signalled but refused" are visible instead of guessed.
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path


def file_hash(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class ReloadStatus:
    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self.path = None
        self.applied = None
        self.last_attempt = None

    def applied_now(self, path, digest, principals, source):
        with self._lock:
            self.path = str(path)
            self.applied = dict(at=round(self._clock(), 3), hash=digest, principals=principals, source=source)
            self.last_attempt = dict(at=self.applied['at'], outcome='applied', hash=digest, reason=None,
                                     source=source)

    def refused(self, path, digest, reason):
        with self._lock:
            self.path = str(path)
            self.last_attempt = dict(at=round(self._clock(), 3), outcome='refused', hash=digest,
                                     reason=str(reason)[:300], source='sighup')

    def snapshot(self):
        with self._lock:
            applied, last, path = self.applied, self.last_attempt, self.path
        current = file_hash(path) if path else None
        in_sync = bool(applied) and current is not None and applied['hash'] == current
        refusal_pending = bool(last) and last['outcome'] == 'refused' and last['hash'] == current
        return dict(
            config_path=path, trigger='SIGHUP only; the authority never watches the file',
            applied=applied, last_attempt=last,
            file=dict(hash=current, exists=current is not None),
            in_sync=in_sync,
            verdict=('applied' if in_sync else 'refused_current_file' if refusal_pending else
                     'edited_not_applied' if applied and current else 'unknown'),
            not_reloaded='state_dir, port, limits, blob_limits, artifact_mirror need a restart; claims and '
                         'rollout state live in the database and are not read from the file')
