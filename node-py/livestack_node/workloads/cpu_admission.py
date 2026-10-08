"""worker.json `cpu_admission`: how the worker derives `available.cpu`
(docs/worker-cpu-admission.md). Absent means `loadavg`. Unknown keys fail closed.

Plain Python on purpose: the worker must start on a stock interpreter with no third-party
packages (hand-managed hosts; a missing pydantic crash-looped zz-joe's first roll). The
messages mirror the pydantic ones this replaced. Pydantic stays the authority's dependency
(config.py), never the worker's.
"""
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

POLICIES = ('loadavg', 'psi', 'psi_some', 'runqueue')
SELFTEST_POLICIES = ('psi_some', 'runqueue')   # enforced; `psi` is tested but advisory (legacy)
MIN_SIGNAL_RISE = {'psi_some': 1.0, 'runqueue': 0.5, 'psi': 1.0}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class CpuAdmission:
    policy: str = 'loadavg'
    # psi: the host counts as stalled (report 0) when /proc/pressure/cpu `full avg60`
    # exceeds this percentage; otherwise the whole capacity less reserve_cpu is offered
    # and placement's reservations decide fit.
    stall_full_avg60_percent: float = 5
    # CPUs held back for work Harmony does not place (agents' own runs).
    reserve_cpu: float = 0
    # Test seam and non-standard mounts; the kernel's file by default.
    psi_path: str = '/proc/pressure/cpu'
    # psi_some: stalled when `some avg10` (percent) reaches this. avg10 so a finished burst
    # releases admission within ~10 s.
    stall_some_avg10_percent: float = 40
    # runqueue: stalled when mean runnable tasks per core (own sampler excluded) exceeds this.
    stall_runnable_per_core: float = 2.0
    window_seconds: float = 5
    # Synthetic burn used to verify the signal moves (bounded 1-10 s).
    selftest_seconds: float = 3
    # Where PSI is absent (file missing, not merely quiet), psi_some may be served by this named
    # policy. Announced in the report, never silent. Inert signals never fall back.
    fallback: str | None = 'runqueue'
    proc_root: str = '/proc'

    @classmethod
    def validate(cls, raw):
        """Return a CpuAdmission or raise ValueError('<key>: <message>; ...') without values."""
        if not isinstance(raw, dict):
            raise ValueError('<file>: Input should be a valid dictionary')
        problems = []
        known = ('policy', 'stall_full_avg60_percent', 'reserve_cpu', 'psi_path', 'stall_some_avg10_percent',
                 'stall_runnable_per_core', 'window_seconds', 'selftest_seconds', 'fallback', 'proc_root')
        problems += [f'{key}: Extra inputs are not permitted' for key in raw if key not in known]
        values = {}
        if 'policy' in raw:
            if isinstance(raw['policy'], str) and raw['policy'] in POLICIES:
                values['policy'] = raw['policy']
            else:
                problems.append("policy: Input should be 'loadavg', 'psi', 'psi_some' or 'runqueue'")
        if 'fallback' in raw:
            if raw['fallback'] is None or raw['fallback'] == 'runqueue':
                values['fallback'] = raw['fallback']
            else:
                problems.append("fallback: Input should be 'runqueue' or null")
        for key, upper, lower in (('stall_full_avg60_percent', 100, 0), ('reserve_cpu', None, 0),
                                  ('stall_some_avg10_percent', 100, 0), ('stall_runnable_per_core', None, 0),
                                  ('window_seconds', 60, 1), ('selftest_seconds', 10, 1)):
            if key not in raw:
                continue
            number = raw[key]
            if not _number(number):
                problems.append(f'{key}: Input should be a valid number')
            elif not number >= lower or math.isnan(number):
                problems.append(f'{key}: Input should be greater than or equal to {lower}')
            elif upper is not None and number > upper:
                problems.append(f'{key}: Input should be less than or equal to {upper}')
            else:
                values[key] = number
        for key in ('psi_path', 'proc_root'):
            if key in raw:
                if isinstance(raw[key], str):
                    values[key] = raw[key]
                else:
                    problems.append(f'{key}: Input should be a valid string')
        if problems:
            raise ValueError('; '.join(problems))
        return cls(**values)



# ---- signals -----------------------------------------------------------------
# A signal is a zero-argument callable returning a float, or raising OSError/ValueError/KeyError
# when it cannot be read. Higher is more stalled.

def psi_signal(path, kind, field):
    def read():
        for line in open(path).read().splitlines():
            fields = line.split()
            if fields and fields[0] == kind:
                return float(dict(f.split('=') for f in fields[1:])[field])
        raise KeyError(kind)
    return read


class RunqueueSampler:
    """Mean runnable tasks per core over `window_seconds`, sampled at 1 Hz by a daemon thread.

    `procs_running` counts this thread too, so one is subtracted. Until the first sample the
    value is unreadable (never a made-up zero)."""

    def __init__(self, proc_root='/proc', window_seconds=5, cores=None, interval=1.0):
        self.path, self.cores, self.interval = proc_root.rstrip('/') + '/stat', cores or os.cpu_count() or 1, interval
        self.samples = deque(maxlen=max(1, int(window_seconds / interval)))
        self._stop = threading.Event()
        self._thread = None

    def sample(self):
        for line in open(self.path).read().splitlines():
            if line.startswith('procs_running'):
                self.samples.append(max(0, int(line.split()[1]) - 1))
                return
        raise KeyError('procs_running')

    def start(self):
        self.sample()
        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.sample()
                except (OSError, ValueError, KeyError):
                    self.samples.clear()
        self._thread = threading.Thread(target=loop, daemon=True, name='runqueue-sampler')
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def __call__(self):
        if not self.samples:
            raise KeyError('no runqueue sample yet')
        return sum(self.samples) / len(self.samples) / self.cores


def selftest(signal, *, policy, threshold, cores, seconds, burn=None, poll=0.25):
    """Positive control: does `signal` rise when cores+1 busy processes run for `seconds`?

    Returns dict(state, detail). `inert`: unreadable or did not rise by MIN_SIGNAL_RISE.
    `active_unverified`: the host was already above its stall threshold, so the burn was skipped.
    `active`: the signal rose. `burn` is a test seam: a context-manager factory replacing the
    real processes."""
    try:
        idle = signal()
    except (OSError, ValueError, KeyError) as error:
        return dict(state='inert', detail=f'{policy} unreadable at self-test: {type(error).__name__}')
    if idle >= threshold:
        return dict(state='active_unverified', detail=f'self-test skipped, host busy ({policy} {idle:.2f})')
    peak = idle
    started = time.monotonic()
    procs = []
    try:
        if burn is not None:
            context = burn(cores + 1)
            context.__enter__()
        else:
            procs = [subprocess.Popen([sys.executable, '-c', 'while True: pass'], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(cores + 1)]
        while time.monotonic() - started < seconds:
            time.sleep(poll)
            try:
                peak = max(peak, signal())
            except (OSError, ValueError, KeyError):
                pass
    finally:
        for proc in procs:
            proc.kill()
        for proc in procs:
            proc.wait()
        if burn is not None:
            context.__exit__(None, None, None)
    rise = peak - idle
    need = MIN_SIGNAL_RISE[policy]
    if rise >= need:
        return dict(state='active', detail=f'{policy} rose {idle:.2f} -> {peak:.2f} under {cores + 1} busy processes')
    return dict(state='inert', detail=f'{policy} did not move under {cores + 1} busy processes '
                                      f'({idle:.2f} -> {peak:.2f}, needs +{need:g}); signal cannot gate admission')
