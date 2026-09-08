"""Provision an owned, size-bounded ext4 workspace with a persistent mount.

Run as root on a Linux/WSL host. This never formats a device or an existing file;
only a newly created file beneath /var/lib/livestack-workloads is eligible.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import subprocess


def run(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=180).stdout.strip()


def provision(worker, owner, size_gib):
    if os.geteuid() != 0:
        raise ValueError('workspace provisioning requires root')
    if not re.fullmatch('[a-z0-9][a-z0-9-]{0,39}', worker):
        raise ValueError('worker must be a simple Linux unit name')
    if not isinstance(size_gib, int) or not 1 <= size_gib <= 512:
        raise ValueError('workspace size must be 1..512 GiB')
    account = pwd.getpwnam(owner)
    root = Path('/var/lib/livestack-workloads')/worker
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    if root.resolve() != root or root.stat().st_uid != 0:
        raise ValueError('workspace provisioning root must be private and root-owned')
    image, mount, marker = root/'workspace.ext4', root/'workspace', root/'workspace.json'
    size = size_gib*1024**3
    expected = dict(version=1, worker=worker, owner=owner, uid=account.pw_uid, bytes=size)
    if image.exists():
        if image.is_symlink() or not marker.exists() or json.loads(marker.read_text()) != expected:
            raise ValueError('existing workspace does not match owned provisioning metadata')
        if image.stat().st_size != size or run('blkid', '-o', 'value', '-s', 'TYPE', str(image)) != 'ext4':
            raise ValueError('existing workspace image is incomplete or has changed')
    else:
        if mount.is_mount() or marker.exists():
            raise ValueError('unexpected existing workspace state; refusing to format')
        stats = os.statvfs(root)
        if stats.f_bavail*stats.f_frsize < size + 20*1024**3:
            raise ValueError('insufficient host disk headroom for workspace and reserve')
        print(f'Allocating {size_gib} GiB owned workspace for {worker}', flush=True)
        # Exclusive creation prevents replacing an existing image. Allocation
        # reserves Linux filesystem space; Windows backing-disk headroom must
        # also be monitored by the WSL worker configuration.
        with image.open('xb') as stream:
            stream.truncate(size)
        image.chmod(0o600)
        run('fallocate', '-l', str(size), str(image))
        run('mkfs.ext4', '-q', '-m', '0', '-E', 'nodiscard,lazy_itable_init=1,lazy_journal_init=1', str(image))
        marker.write_text(json.dumps(expected, sort_keys=True)+'\n')
        marker.chmod(0o600)
    mount.mkdir(exist_ok=True, mode=0o755)
    unit = run('systemd-escape', '--path', '--suffix=mount', str(mount))
    text = ('[Unit]\nDescription=Harmony bounded workload workspace\n\n[Mount]\n'
            f'What={image}\nWhere={mount}\nType=ext4\nOptions=loop,nodev,nosuid\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
    path = Path('/etc/systemd/system')/unit
    if path.exists() and path.read_text() != text:
        raise ValueError('existing mount unit differs; refusing to replace it')
    path.write_text(text)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', unit)
    if not mount.is_mount() or mount.stat().st_dev == root.stat().st_dev:
        raise ValueError('workspace mount did not become independent')
    source = run('findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(mount))
    backing = run('losetup', '-n', '-O', 'BACK-FILE', source)
    if Path(backing).resolve() != image:
        raise ValueError('mounted filesystem is not the owned workspace image')
    os.chown(mount, account.pw_uid, account.pw_gid)
    mount.chmod(0o700)
    result = dict(workspace=str(mount), workspace_bytes=size, mount_unit=unit,
                  filesystem_bytes=os.statvfs(mount).f_blocks*os.statvfs(mount).f_frsize)
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True)
    parser.add_argument('--owner', default='ubuntu')
    parser.add_argument('--size-gib', type=int, default=128)
    args = parser.parse_args()
    provision(args.worker, args.owner, args.size_gib)


if __name__ == '__main__':
    main()
