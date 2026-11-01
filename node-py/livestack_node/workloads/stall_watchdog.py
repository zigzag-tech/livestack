"""In-process stall watchdog: a worker that stops making progress fails closed.

The worker's main thread does everything: register, claim, prepare, supervise,
hand off. Every network call in it carries a socket timeout, but a wedge in any
call that does not (a blocked local lock, a hung child process, a stuck
filesystem) leaves the process alive and silent: the authority sees the worker
OFFLINE while it still holds a claimed assignment (2026-10-15, xc-win-1-wsl-2).
The main thread therefore announces each phase with a deadline (`beat`); this
thread names the phase, logs every thread's stack (the evidence a wedge
otherwise destroys) and exits the process non-zero. The service manager
restarts it and the journal reconciles the held attempt, which the authority
then sees as lost, not silently held. docs/worker-claims-and-rollout.md,
"Stall watchdog".
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback

EXIT_CODE = 70  # EX_SOFTWARE: distinguishes a stall exit in `systemctl status`


class StallWatchdog:
    def __init__(self, *, poll=5.0, on_stall=None, clock=time.monotonic):
        self.poll, self.clock = poll, clock
        self.on_stall = on_stall or self._exit
        self._lock = threading.Lock()
        self._phase, self._limit, self._since = 'starting', 600.0, clock()
        self._stop = threading.Event()
        self.thread = None

    def beat(self, phase, limit):
        """The main thread is in `phase` and owes another beat within `limit` seconds."""
        with self._lock:
            if phase != self._phase:
                logging.info('worker phase: %s -> %s (stall limit %.0fs)', self._phase, phase, limit)
            self._phase, self._limit, self._since = phase, float(limit), self.clock()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True, name='worker-stall-watchdog')
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def check(self):
        with self._lock:
            phase, limit, waited = self._phase, self._limit, self.clock()-self._since
        if waited <= limit:
            return False
        logging.error('worker stalled in phase %r for %.0fs (limit %.0fs); failing closed so the '
                      'service manager restarts it and the held attempt is reported lost.\n%s',
                      phase, waited, limit, self._stacks())
        self.on_stall(phase, waited)
        return True

    def _run(self):
        while not self._stop.wait(self.poll):
            if self.check():
                return

    @staticmethod
    def _stacks():
        names = {t.ident: t.name for t in threading.enumerate()}
        return ''.join('--- thread %s\n%s' % (names.get(ident, ident), ''.join(traceback.format_stack(frame)))
                       for ident, frame in sys._current_frames().items())[-12000:]

    @staticmethod
    def _exit(phase, waited):
        logging.shutdown()
        os._exit(EXIT_CODE)
