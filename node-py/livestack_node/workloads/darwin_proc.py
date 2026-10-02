"""macOS process facts for supervision and launch verification (libproc).

Stdlib only and importable as a sibling module: `bounded_exec.py` runs as a
script and imports it by file name. Python 3.9 compatible, because the root
verifier runs on the root-owned /usr/bin/python3 (a user-writable interpreter
must never run as root).

What this replaces from Linux: a cgroup. On macOS a process belongs to an
attempt iff its parent chain reaches the attempt's launchd job PID with the same
start time, or it is still in that job's process group (openspec change
apple-host-compilation, design "Containment").
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import struct
import subprocess
import sys

PROC_PIDTBSDINFO = 3
RUSAGE_INFO_V0 = 0
# <sys/un.h>: SOL_LOCAL 0, LOCAL_PEERCRED 0x001, LOCAL_PEERPID 0x002.
SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
LOCAL_PEERPID = 0x002
# Bounds: one host's process table, and one attempt's tree.
MAX_PIDS = 65536
MAX_TREE = 4096


class _BsdInfo(ctypes.Structure):
    _fields_ = [('pbi_flags', ctypes.c_uint32), ('pbi_status', ctypes.c_uint32),
                ('pbi_xstatus', ctypes.c_uint32), ('pbi_pid', ctypes.c_uint32),
                ('pbi_ppid', ctypes.c_uint32), ('pbi_uid', ctypes.c_uint32),
                ('pbi_gid', ctypes.c_uint32), ('pbi_ruid', ctypes.c_uint32),
                ('pbi_rgid', ctypes.c_uint32), ('pbi_svuid', ctypes.c_uint32),
                ('pbi_svgid', ctypes.c_uint32), ('rfu_1', ctypes.c_uint32),
                ('pbi_comm', ctypes.c_char*16), ('pbi_name', ctypes.c_char*32),
                ('pbi_nfiles', ctypes.c_uint32), ('pbi_pgid', ctypes.c_uint32),
                ('pbi_pjobc', ctypes.c_uint32), ('e_tdev', ctypes.c_uint32),
                ('e_tpgid', ctypes.c_uint32), ('pbi_nice', ctypes.c_int32),
                ('pbi_start_tvsec', ctypes.c_uint64), ('pbi_start_tvusec', ctypes.c_uint64)]


class _RusageV0(ctypes.Structure):
    _fields_ = [('ri_uuid', ctypes.c_uint8*16), ('ri_user_time', ctypes.c_uint64),
                ('ri_system_time', ctypes.c_uint64), ('ri_pkg_idle_wkups', ctypes.c_uint64),
                ('ri_interrupt_wkups', ctypes.c_uint64), ('ri_pageins', ctypes.c_uint64),
                ('ri_wired_size', ctypes.c_uint64), ('ri_resident_size', ctypes.c_uint64),
                ('ri_phys_footprint', ctypes.c_uint64), ('ri_proc_start_abstime', ctypes.c_uint64),
                ('ri_proc_exit_abstime', ctypes.c_uint64)]


_LIB = None


def _lib():
    global _LIB
    if sys.platform != 'darwin':
        raise OSError('libproc is macOS only')
    if _LIB is None:
        lib = ctypes.CDLL(ctypes.util.find_library('proc') or '/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        _LIB = lib
    return _LIB


def info(pid):
    """{'pid','ppid','pgid','uid','start'} or None when the process is gone or
    not inspectable. `start` is (sec, usec): with the PID it names one process."""
    record = _BsdInfo()
    size = _lib().proc_pidinfo(int(pid), PROC_PIDTBSDINFO, 0, ctypes.byref(record), ctypes.sizeof(record))
    if size != ctypes.sizeof(record):
        return None
    return dict(pid=record.pbi_pid, ppid=record.pbi_ppid, pgid=record.pbi_pgid, uid=record.pbi_uid,
                start=(record.pbi_start_tvsec, record.pbi_start_tvusec))


def all_pids():
    buffer = (ctypes.c_int*MAX_PIDS)()
    count = _lib().proc_listallpids(buffer, ctypes.sizeof(buffer))
    if count <= 0:
        raise OSError(ctypes.get_errno(), 'proc_listallpids failed')
    return [p for p in buffer[:min(count, MAX_PIDS)] if p > 0]


def footprint(pid):
    """Physical footprint in bytes (anonymous + compressed + wired, not file
    cache): the macOS analogue of a cgroup's non-reclaimable memory. None when
    the process is gone or not inspectable."""
    usage = _RusageV0()
    if _lib().proc_pid_rusage(int(pid), RUSAGE_INFO_V0, ctypes.byref(usage)) != 0:
        return None
    return usage.ri_phys_footprint


def tree(root_pid, root_start, *, group=True):
    """Processes of the attempt rooted at (root_pid, root_start): descendants by
    parent chain plus, when `group`, members of the root's process group.
    Empty when the root is gone or is another process now using that PID."""
    root = info(root_pid)
    if root is None or root['start'] != tuple(root_start):
        return {}
    table = {}
    for pid in all_pids():
        record = info(pid)
        if record is not None:
            table[pid] = record
    children = {}
    for record in table.values():
        children.setdefault(record['ppid'], []).append(record['pid'])
    found, pending = {root_pid: root}, [root_pid]
    while pending and len(found) < MAX_TREE:
        for child in children.get(pending.pop(), ()):
            if child not in found and child != root_pid:
                found[child] = table[child]
                pending.append(child)
    if group:
        for record in table.values():
            if record['pgid'] == root['pgid'] and len(found) < MAX_TREE:
                found.setdefault(record['pid'], record)
    return found


def descends_from(pid, ancestor_pid, ancestor_start, *, limit=256):
    """True iff `pid`'s parent chain reaches (ancestor_pid, ancestor_start)."""
    current = info(pid)
    for _ in range(limit):
        if current is None:
            return False
        if current['pid'] == ancestor_pid:
            return current['start'] == tuple(ancestor_start)
        if current['ppid'] in (0, current['pid']):
            return False
        current = info(current['ppid'])
    return False


def peer_credentials(connection):
    """(pid, uid) of a connected AF_UNIX peer, from the kernel."""
    pid = struct.unpack('i', connection.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
    # struct xucred: u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]
    raw = connection.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, 76)
    version, uid = struct.unpack_from('II', raw, 0)
    if version != 0:
        raise OSError('unsupported xucred version')
    return pid, uid


def platform_uuid():
    """IOPlatformUUID lowercased without dashes: 32 hex, the machine-id shape."""
    text = subprocess.run(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                          check=True, capture_output=True, text=True, timeout=5).stdout
    match = re.search(r'"IOPlatformUUID" = "([0-9A-Fa-f-]{36})"', text)
    if not match:
        raise OSError('IOPlatformUUID unavailable')
    return match.group(1).replace('-', '').lower()


def memory_total():
    return int(subprocess.run(['/usr/sbin/sysctl', '-n', 'hw.memsize'], check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip())


def kill_tree(found, signal_number):
    for pid, record in found.items():
        current = info(pid)
        if current is not None and current['start'] == record['start']:
            try:
                os.kill(pid, signal_number)
            except ProcessLookupError:
                pass


def survivors(found):
    return {pid: r for pid, r in found.items() if (info(pid) or {}).get('start') == r['start']}
