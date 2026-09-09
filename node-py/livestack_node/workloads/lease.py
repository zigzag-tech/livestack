"""Worker-owned renewals with a monotonic deadline enforced inside the unit."""
from __future__ import annotations

import math
import os
from pathlib import Path
from threading import Event, Thread
import time

from .model import WorkloadError


class LeaseKeeper:
    def __init__(self, client, assignment, path, *, interval=10):
        self.client, self.assignment = client, assignment
        self.path = Path(path)
        self.interval = interval
        self.stopped = Event()
        self.lost = Event()
        self.thread = None
        self.error = None
        self.remaining = 0
        self.deadline = 0

    def _write(self, deadline):
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w') as out:
            out.write(str(deadline))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)

    def renew(self):
        started = time.monotonic()
        a = self.assignment
        result = self.client.request('worker/heartbeat', dict(
            boot=a['boot'], attempt_id=a['attempt_id'], fence=a['fence']))
        remaining = result.get('lease_remaining')
        if not isinstance(remaining, (float, int)) or not math.isfinite(remaining) or remaining <= 0:
            raise WorkloadError('authority did not provide a finite lease duration', 503)
        # Count the request's full round trip against the duration. Authority
        # and worker clocks need not agree. Leave time for wrapper/cgroup stop.
        deadline = started + remaining - min(1, remaining/4)
        self.deadline = deadline
        self.remaining = deadline-time.monotonic()
        if self.remaining <= 0:
            raise WorkloadError('renewal arrived after its safe deadline', 503)
        self._write(deadline)

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.renew()  # No process can start without a fresh grant.
        self.thread = Thread(target=self._loop, daemon=True, name='harmony-lease')
        self.thread.start()
        return self

    def _lose(self, error):
        self.error = error
        try:
            self._write(0)
        except OSError:
            pass
        finally:
            self.lost.set()

    def _loop(self):
        retrying = False
        while True:
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                self._lose(self.error or 'LeaseExpired')
                return
            delay = min(1 if retrying else self.interval, remaining/3)
            if self.stopped.wait(delay):
                return
            try:
                self.renew()
                retrying = False
            except WorkloadError as error:
                # An explicit authority refusal revokes the lease immediately.
                # Transport/server failures cannot extend it, but may retry
                # within the deadline already granted by the authority.
                if 400 <= error.status < 500:
                    self._lose(type(error).__name__)
                    return
                self.error = type(error).__name__
                retrying = True
            except Exception as error:
                self.error = type(error).__name__
                retrying = True

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=self.client.timeout+1)
            if self.thread.is_alive():
                raise WorkloadError('renewal thread did not stop', 503)
        self._write(0)
