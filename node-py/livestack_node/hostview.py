"""The measured view of one host, for every Harmony planner that places work on it.

Stdlib only: workload workers run on the system python3. Every reader takes its
root (`proc`, `cgroup_root`) so tests drive it with real files in a temp tree.

Why this exists (openspec/changes/host-memory-ledger): on 2026-10-02 zz-joe
swapped 17 GiB and every e2e attempt there failed at startup. Placement charged
two e2e attempts their 4 GiB `admit` while each reached 10 GiB, and the image
model server's 16 GB host-RAM transient was in no ledger at all. This module
reports what the host has (MemAvailable, PSI, swap-in) and what every Harmony
tenant on it holds now and has been seen to reach.

Unknown is never zero: a reading that cannot be taken is None.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

# harmony-work-<sha256(worker)[:16]>-<attempt id>.service (supervision.py).
ATTEMPT_UNIT = re.compile(r'harmony-work-[0-9a-f]{16}-([0-9a-f]{32})\.service')
MAX_SERVICES = 32
MAX_ATTEMPTS = 64


def meminfo(proc='/proc'):
    """{'total': bytes, 'available': bytes} from /proc/meminfo."""
    fields = {}
    for line in Path(proc, 'meminfo').read_text().splitlines():
        key, _, rest = line.partition(':')
        fields[key] = int(rest.split()[0])*1024
    return {'total': fields['MemTotal'], 'available': fields['MemAvailable']}


def psi(resource, proc='/proc'):
    """{'some_avg10', 'some_avg60', 'full_avg10', 'full_avg60'} or None without PSI.

    The cpu file has no `full` line on older kernels; its full_* are then None.
    """
    try:
        text = Path(proc, 'pressure', resource).read_text()
    except OSError:
        return None
    out = {'some_avg10': None, 'some_avg60': None, 'full_avg10': None, 'full_avg60': None}
    for line in text.splitlines():
        kind, *pairs = line.split()
        values = dict(pair.split('=', 1) for pair in pairs)
        for window in ('avg10', 'avg60'):
            if kind in ('some', 'full') and window in values:
                out[f'{kind}_{window}'] = float(values[window])
    return out


def swap_in_pages(proc='/proc'):
    try:
        for line in Path(proc, 'vmstat').read_text().splitlines():
            key, _, value = line.partition(' ')
            if key == 'pswpin':
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def cgroup_memory(path):
    """(memory.current, memory.peak) of one cgroup v2 dir; None when it is absent.

    memory.peak exists from kernel 5.19; without it the peak is None.
    """
    path = Path(path)
    try:
        current = int((path/'memory.current').read_text())
    except (OSError, ValueError):
        return None
    try:
        peak = int((path/'memory.peak').read_text())
    except (OSError, ValueError):
        peak = None
    return current, peak


def user_app_slice(cgroup_root='/sys/fs/cgroup', uid=None):
    """Where `systemd-run --user` (supervision.py) puts every attempt unit of
    this user's workers, siblings included."""
    uid = os.getuid() if uid is None else uid
    return Path(cgroup_root)/f'user.slice/user-{uid}.slice/user@{uid}.service/app.slice'


class HostView:
    """Samples the host for a worker report and remembers what it has learned.

    services: cgroup paths relative to cgroup_root of model servers Harmony runs on
    this host (e.g. "system.slice/harmony-klein-0.service"). Their learned peak is
    the max memory.peak ever seen, persisted in peaks_path so a restart, which
    resets memory.peak, does not forget a transient.
    """

    def __init__(self, *, services=(), peaks_path=None, attempts_dir=None,
                 reserve_bytes=1024**3, proc='/proc', cgroup_root='/sys/fs/cgroup', clock=time.monotonic):
        services = list(services)
        if len(services) > MAX_SERVICES or any(
                not isinstance(s, str) or not s or s.startswith('/') or '..' in s.split('/') for s in services):
            raise ValueError(f'host_services must be at most {MAX_SERVICES} relative cgroup paths')
        self.services = services
        self.peaks_path = Path(peaks_path) if peaks_path else None
        self.attempts_dir = attempts_dir
        self.reserve_bytes = int(reserve_bytes)
        self.proc, self.cgroup_root, self.clock = proc, Path(cgroup_root), clock
        self._swap = None  # (clock, pswpin)
        self.peaks = {}
        if self.peaks_path and self.peaks_path.exists():
            try:
                stored = json.loads(self.peaks_path.read_text())
                self.peaks = {k: int(v) for k, v in stored.items() if k in services}
            except (OSError, ValueError, TypeError, AttributeError):
                self.peaks = {}

    def _swap_rate(self):
        pages, now = swap_in_pages(self.proc), self.clock()
        previous, self._swap = self._swap, (None if pages is None else (now, pages))
        if pages is None or previous is None or now <= previous[0]:
            return None
        return max(0, pages-previous[1])*os.sysconf('SC_PAGE_SIZE')/(now-previous[0])

    def _attempts(self):
        directory = self.attempts_dir if self.attempts_dir is not None else user_app_slice(self.cgroup_root)
        out = {}
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return out
        for entry in entries:
            match = ATTEMPT_UNIT.fullmatch(entry.name)
            if match and len(out) < MAX_ATTEMPTS:
                reading = cgroup_memory(entry.path)
                if reading is not None:
                    out[match.group(1)] = reading[0]
        return out

    def _services(self):
        out, changed = {}, False
        for service in self.services:
            reading = cgroup_memory(self.cgroup_root/service)
            current, peak = (0, None) if reading is None else reading
            learned = max(self.peaks.get(service, 0), peak or 0, current)
            if learned != self.peaks.get(service):
                self.peaks[service], changed = learned, True
            out[service] = {'current_bytes': current, 'peak_bytes': learned}
        if changed and self.peaks_path:
            tmp = self.peaks_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.peaks, sort_keys=True))
            tmp.replace(self.peaks_path)
        return out

    def sample(self):
        memory = meminfo(self.proc)
        return {
            'memory_total_bytes': memory['total'],
            'memory_available_bytes': memory['available'],
            'memory_reserve_bytes': self.reserve_bytes,
            'swap_in_bytes_per_second': self._swap_rate(),
            'psi': {r: psi(r, self.proc) for r in ('memory', 'io', 'cpu')},
            'attempts': self._attempts(),
            'services': self._services(),
        }
