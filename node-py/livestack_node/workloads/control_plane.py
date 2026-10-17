"""Name a failed host-control-plane call instead of reporting only its exit status.

2026-10-17 incident: a scratch GUI process leaked system-bus connections until its UID reached
dbus-daemon's max_connections_per_user (256). Every `systemctl --system` call of every worker on
that host then failed with ENOBUFS, and the log said only "returned non-zero exit status 1" (the
captured stderr was dropped), so six healthy-looking workers sat silent for two hours with no
named cause. `HostCommandFailed` keeps the stderr and the cause; `top_socket_holders` names the
processes that hold the sockets when the cause is connection exhaustion.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

CAUSE_CONTROL_PLANE = 'host_control_plane_unavailable'
_SIGNATURES = ('No buffer space available', 'Failed to connect to bus', 'Transport endpoint is not connected',
               'Connection refused', 'The maximum number of active connections')


def classify(stderr):
    """`host_control_plane_unavailable` when systemd or its bus refused the call, else None."""
    text = stderr or ''
    return CAUSE_CONTROL_PLANE if any(sig in text for sig in _SIGNATURES) else None


def top_socket_holders(limit=3, proc=Path('/proc'), uid=None):
    """(count, pid, command) of this user's processes holding the most sockets; Linux only, never raises."""
    uid = os.getuid() if uid is None and hasattr(os, 'getuid') else uid
    found = []
    try:
        entries = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.stat().st_uid != uid:
                continue
            count = 0
            for fd in os.listdir(entry/'fd'):
                try:  # an fd closed between listing and reading is not a reason to skip the process
                    count += os.readlink(entry/'fd'/fd).startswith('socket:')
                except OSError:
                    pass
            command = (entry/'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:
            continue
        if count:
            found.append((count, int(entry.name), command[:80]))
    return sorted(found, reverse=True)[:limit]


class HostCommandFailed(subprocess.CalledProcessError):
    """A CalledProcessError whose text carries the stderr and, when known, the cause."""
    def __init__(self, error):
        super().__init__(error.returncode, error.cmd, error.output, error.stderr)
        self.cause = classify(error.stderr)

    def __str__(self):
        text = f"Command '{self.cmd}' returned non-zero exit status {self.returncode}"
        if self.stderr:
            text += f": {self.stderr.strip()[:200]}"
        if self.cause:
            text += f' [cause={self.cause}'
            if 'No buffer space' in (self.stderr or '') or 'maximum number' in (self.stderr or ''):
                holders = top_socket_holders()
                text += '; top socket holders: ' + ', '.join(f'pid {p} {n} sockets {c}' for n, p, c in holders)
            text += ']'
        return text
