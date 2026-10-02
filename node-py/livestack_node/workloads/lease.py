"""Worker-owned renewals with a monotonic deadline enforced inside the unit."""
from __future__ import annotations

import http.client
import json
import logging
import math
import os
from pathlib import Path
from threading import Event, Thread
import time
import urllib.error

from .model import WorkloadError, progress as validate_progress


def transient(error):
    """True for a failure that says only "the authority could not be reached
    just now": connection refused/reset, timeout, or a 502/503/504 from the
    edge. An answer from the authority (409 fence, 404, ...) is never transient,
    and neither is a local fault."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (502, 503, 504)
    if isinstance(error, WorkloadError):
        return error.status in (502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException))


def retry_transient(call, what, *, budget, keep_going=lambda: True, first_delay=.5, max_delay=5):
    """Run call(), retrying transient failures with bounded backoff for at most
    `budget` seconds while keep_going() holds. A non-transient failure, or the
    last transient one once the budget is spent, is raised unchanged after a
    log line naming the cause."""
    give_up = time.monotonic()+budget
    delay = first_delay
    while True:
        try:
            return call()
        except Exception as error:
            left = give_up-time.monotonic()
            if not transient(error) or left <= 0 or not keep_going():
                if transient(error):
                    logging.warning('%s failed, not retrying: %s: %s', what, type(error).__name__, error)
                raise
            logging.warning('%s: authority unreachable (%s: %s); retrying in %.1fs (%.0fs of budget left)',
                            what, type(error).__name__, error, min(delay, left), left)
            time.sleep(min(delay, left))
            delay = min(delay*2, max_delay)


class LeaseKeeper:
    def __init__(self, client, assignment, path, *, interval=10, progress_path=None, start_retry_seconds=15):
        # Renewals ride a connection of their own, opened by the initial grant
        # in start(): never queued behind the worker's claim/report/complete,
        # and no new handshake per renewal (openspec/changes/worker-control-keepalive).
        self.client, self.assignment = client.channel(), assignment
        self.path = Path(path)
        self.interval = interval
        self.start_retry_seconds = start_retry_seconds
        self._noted = None
        self.progress_path = Path(progress_path) if progress_path is not None else None
        self.progress_seen = None
        self.stopped = Event()
        self.lost = Event()
        self.thread = None
        self.error = None
        self.remaining = 0
        self.deadline = 0
        self.liveness = None

    def _read_progress(self):
        """The handler's latest progress.json, sent only when it changed. A
        malformed file is ignored, never a reason to skip a renewal."""
        if self.progress_path is None:
            return None
        try:
            raw = self.progress_path.read_text()
        except OSError:
            return None
        if raw == self.progress_seen:
            return None
        try:
            value = validate_progress(json.loads(raw))
        except (ValueError, WorkloadError):
            return None
        self.progress_seen = raw
        return value

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
        body = dict(boot=a['boot'], attempt_id=a['attempt_id'], fence=a['fence'])
        progress = self._read_progress()
        if progress is not None:
            body['progress'] = progress
        result = self.client.request('worker/heartbeat', body)
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
        # No process can start without a fresh grant. The authority may be
        # mid-restart at the moment of the claim: retry briefly, never forever.
        retry_transient(self.renew, 'initial lease grant', budget=self.start_retry_seconds)
        self.thread = Thread(target=self._loop, daemon=True, name='harmony-lease')
        self.thread.start()
        return self

    def require_liveness(self, predicate):
        if not callable(predicate):
            raise WorkloadError('lease liveness predicate must be callable')
        self.liveness = predicate

    def _unreachable(self, error):
        """Name the outage without flooding: first failure, then every 10 s."""
        self.error = type(error).__name__
        now = time.monotonic()
        if self._noted is None or now-self._noted >= 10:
            logging.warning('authority unreachable, retrying (lease has %.0fs left): %s: %s',
                            max(0, self.deadline-now), type(error).__name__, error)
            self._noted = now

    def _lose(self, error):
        logging.warning('lease lost, stopping the attempt: %s', error)
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
                self._lose('LeaseExpired: no successful renewal before the granted deadline (last error: %s)'
                           % (self.error or 'none'))
                return
            delay = min(1 if retrying else self.interval, remaining/3)
            if self.stopped.wait(delay):
                return
            predicate = self.liveness
            if predicate is not None:
                try:
                    alive = predicate()
                except Exception as error:
                    self._lose(type(error).__name__)
                    return
                if alive is not True:
                    self._lose('WorkNotAlive')
                    return
            try:
                self.renew()
                if retrying:
                    logging.info('authority reachable again, lease renewed (%.0fs left)', self.remaining)
                retrying = False
                self._noted = None
            except WorkloadError as error:
                # An explicit authority refusal revokes the lease immediately.
                # Transport/server failures cannot extend it, but may retry
                # within the deadline already granted by the authority.
                if 400 <= error.status < 500:
                    self._lose('%s: authority refused renewal (HTTP %s): %s' % (type(error).__name__, error.status, error))
                    return
                self._unreachable(error)
                retrying = True
            except Exception as error:
                self._unreachable(error)
                retrying = True

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=self.client.timeout+1)
            if self.thread.is_alive():
                raise WorkloadError('renewal thread did not stop', 503)
        self.client.close()
        self._write(0)
