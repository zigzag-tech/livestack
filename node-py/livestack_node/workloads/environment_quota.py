#!/usr/bin/python3
"""Narrow privileged helper for ext4 project quotas on the environment volume.

Install a root-owned copy at the path granted by sudoers. The worker can only
assign a bounded project quota to one direct child named by an authority
handle; it cannot choose a filesystem, quota size above the operator ceiling,
or an arbitrary path.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys

CONFIG = Path('/etc/livestack/workload-environment-quota.json')
MAX_BYTES = 32 * 1024**3
MAX_CONFIG_BYTES = 4096
FS_IOC_FSGETXATTR = 0x801c581f
FS_IOC_FSSETXATTR = 0x401c5820
FS_XFLAG_PROJINHERIT = 0x00000200
PRJQUOTA = 2
Q_SETQUOTA = 0x800008
Q_GETQUOTA = 0x800007
QIF_BLIMITS = 1
QIF_ALL = 63


class IfDqblk(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'dqb_bhardlimit', 'dqb_bsoftlimit', 'dqb_curspace', 'dqb_ihardlimit',
        'dqb_isoftlimit', 'dqb_curinodes', 'dqb_btime', 'dqb_itime')]
    _fields_.append(('dqb_valid', ctypes.c_uint32))


def _quotactl(command, root, project_id, quota):
    libc = ctypes.CDLL(None, use_errno=True)
    operation = (command << 8) | PRJQUOTA
    device = _quota_device(root)
    if libc.quotactl(operation, os.fsencode(device), project_id, ctypes.byref(quota)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(device))


def _quota_device(root):
    result = subprocess.run(['/usr/bin/findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(root)],
                            check=True, capture_output=True, text=True, timeout=3)
    device = result.stdout.strip()
    if not device.startswith('/') or '[' in device or ']' in device:
        raise ValueError('quota root is not a direct block-device mount')
    info = os.stat(device)
    root_device = os.stat(root).st_dev
    if not stat.S_ISBLK(info.st_mode) or os.makedev(os.major(root_device), os.minor(root_device)) != info.st_rdev:
        raise ValueError('quota root mount device does not match its configured filesystem')
    return device


def _load_config():
    if os.geteuid() != 0:
        raise PermissionError('environment quota helper requires root')
    info = CONFIG.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o022 or info.st_size > MAX_CONFIG_BYTES):
        raise ValueError('environment quota config is not private root-owned metadata')
    config = json.loads(CONFIG.read_bytes())
    if (not isinstance(config, dict) or set(config) != {'version', 'root', 'max_bytes', 'project_id_min', 'project_id_max'} or
            config['version'] != 1 or not isinstance(config['root'], str) or not config['root'].startswith('/') or
            isinstance(config['max_bytes'], bool) or not isinstance(config['max_bytes'], int) or
            not 1 <= config['max_bytes'] <= MAX_BYTES or
            type(config['project_id_min']) is not int or type(config['project_id_max']) is not int or
            not 1 <= config['project_id_min'] <= config['project_id_max'] <= 0x7fffffff):
        raise ValueError('invalid environment quota config')
    root = Path(config['root'])
    if root.resolve() != root or not root.is_dir() or root.is_symlink():
        raise ValueError('environment quota root is not a real directory')
    return config, root


def _project_attrs(fd, project_id):
    raw = bytearray(28)
    fcntl.ioctl(fd, FS_IOC_FSGETXATTR, raw, True)
    flags, extsize, nextents, current_id, cowextsize = struct.unpack_from('=IIIII', raw)
    if current_id not in (0, project_id):
        raise ValueError('environment directory already has a different project id')
    if current_id == 0:
        if os.listdir(fd):
            raise ValueError('cannot quota an existing untracked environment directory')
        updated = struct.pack('=IIIII8s', flags | FS_XFLAG_PROJINHERIT, extsize, nextents,
                              project_id, cowextsize, b'\0' * 8)
        fcntl.ioctl(fd, FS_IOC_FSSETXATTR, updated)


def _set_quota(root, project_id, bytes_limit):
    # Quota block limits are expressed in 1 KiB units. Round up, never down.
    blocks = math.ceil(bytes_limit / 1024)
    quota = IfDqblk()
    quota.dqb_bhardlimit = blocks
    quota.dqb_bsoftlimit = blocks
    quota.dqb_valid = QIF_BLIMITS
    _quotactl(Q_SETQUOTA, root, project_id, quota)
    measured = IfDqblk()
    _quotactl(Q_GETQUOTA, root, project_id, measured)
    if measured.dqb_bhardlimit != blocks:
        raise ValueError('kernel did not retain the requested project hard limit')
    if measured.dqb_curspace > bytes_limit:
        raise ValueError('existing environment data exceeds its project limit')
    return measured.dqb_curspace


def ensure(handle, project_id, bytes_limit):
    config, root = _load_config()
    if not re.fullmatch(r'[a-f0-9]{32}', handle):
        raise ValueError('invalid environment handle')
    if type(project_id) is not int or not config['project_id_min'] <= project_id <= config['project_id_max']:
        raise ValueError('project id outside operator range')
    if type(bytes_limit) is not int or not 1 <= bytes_limit <= min(config['max_bytes'], MAX_BYTES):
        raise ValueError('environment quota outside operator limit')
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_info = os.fstat(root_fd)
        target_fd = os.open(handle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            target_info = os.fstat(target_fd)
            if target_info.st_dev != root_info.st_dev:
                raise ValueError('environment target is not on the configured quota filesystem')
            _project_attrs(target_fd, project_id)
        finally:
            os.close(target_fd)
    finally:
        os.close(root_fd)
    used = _set_quota(root, project_id, bytes_limit)
    return dict(handle=handle, project_id=project_id, quota_bytes=bytes_limit, used_bytes=used)


def probe():
    config, root = _load_config()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.ioctl(fd, FS_IOC_FSGETXATTR, bytearray(28), True)
    finally:
        os.close(fd)
    measured = IfDqblk()
    _quotactl(Q_GETQUOTA, root, config['project_id_min'], measured)
    return dict(root=str(root), project_quota=True, project_id_min=config['project_id_min'],
                max_bytes=config['max_bytes'])


def usage(project_ids):
    config, root = _load_config()
    if (not isinstance(project_ids, list) or not 1 <= len(project_ids) <= 64 or
            any(type(value) is not int or not config['project_id_min'] <= value <= config['project_id_max']
                for value in project_ids) or len(set(project_ids)) != len(project_ids)):
        raise ValueError('invalid or oversized project usage request')
    rows = []
    for project_id in project_ids:
        measured = IfDqblk()
        _quotactl(Q_GETQUOTA, root, project_id, measured)
        rows.append(dict(project_id=project_id, used_bytes=measured.dqb_curspace,
                         hard_bytes=measured.dqb_bhardlimit*1024))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('probe')
    measure = commands.add_parser('usage')
    measure.add_argument('project_ids', nargs='+', type=int)
    assign = commands.add_parser('ensure')
    assign.add_argument('handle')
    assign.add_argument('project_id', type=int)
    assign.add_argument('bytes_limit', type=int)
    args = parser.parse_args(argv)
    if args.operation == 'probe':
        result = probe()
    elif args.operation == 'usage':
        result = usage(args.project_ids)
    elif args.operation == 'ensure':
        result = ensure(args.handle, args.project_id, args.bytes_limit)
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, PermissionError) as error:
        print(f'environment quota refused: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(2)
