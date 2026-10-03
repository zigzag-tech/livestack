#!/usr/bin/env python3
"""Positive control for the Linux task-environment project quota backend.

Run as root on an admitted Linux worker host. The script creates a temporary
128 MiB ext4 project-quota image, verifies a child write hits EDQUOT, verifies
that a second environment still has its own quota, unmounts, and deletes it.
It never touches a host device or production worker path.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile

from livestack_node.workloads import environment_quota


def run(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30)


def child_write(account, directory, first_mib, second_mib=0):
    code = f"from pathlib import Path\np=Path({str(directory)!r})\n"
    code += f"(p/'first.bin').write_bytes(b'x'*({first_mib}*1024**2))\n"
    if second_mib:
        code += (f"try:\n (p/'second.bin').write_bytes(b'y'*({second_mib}*1024**2))\n"
                 "except OSError as error:\n print(error.errno)\n"
                 " raise SystemExit(0 if error.errno == 122 else 2)\n"
                 "raise SystemExit(3)\n")
    return run('runuser', '-u', account.pw_name, '--', sys.executable, '-c', code)


def control(owner):
    if os.geteuid() != 0:
        raise RuntimeError('run this positive control as root')
    account = pwd.getpwnam(owner)
    scratch = Path(tempfile.mkdtemp(prefix='task-environment-quota-', dir='/tmp'))
    scratch.chmod(0o755)
    image, mount, config = scratch/'quota.ext4', scratch/'mount', scratch/'quota.json'
    mounted = False
    try:
        size = 128 * 1024**2
        with image.open('xb') as stream:
            stream.truncate(size)
        run('fallocate', '-l', str(size), str(image))
        run('mkfs.ext4', '-F', '-q', '-m', '0', '-O', 'project,quota', str(image))
        mount.mkdir()
        run('mount', '-o', 'loop,nodev,nosuid,prjquota', str(image), str(mount))
        mounted = True
        config.write_text(json.dumps(dict(version=1, root=str(mount), max_bytes=8*1024**2,
            project_id_min=100000, project_id_max=100999)))
        config.chmod(0o600)
        old_config = environment_quota.CONFIG
        environment_quota.CONFIG = config
        try:
            capability = environment_quota.probe()
            handles = ('a'*32, 'b'*32)
            directories = [mount/handle for handle in handles]
            for index, directory in enumerate(directories):
                directory.mkdir()
                environment_quota.ensure(handles[index], 100000+index, 8*1024**2)
                shutil.chown(directory, user=account.pw_uid, group=account.pw_gid)

            exceeded = child_write(account, directories[0], 6, 4)
            if exceeded.stdout.strip() != '122':
                raise RuntimeError('quota overflow control did not report EDQUOT: ' + exceeded.stdout.strip())
            isolated = child_write(account, directories[1], 7)
            if isolated.returncode != 0:
                raise RuntimeError('a second environment lost its independent quota: ' + isolated.stderr.strip())

            usage = environment_quota.usage([100000, 100001])
            project_usage = [row['used_bytes'] for row in usage]
            if (not (0 < project_usage[0] <= 8*1024**2 and
                    7*1024**2 <= project_usage[1] <= 8*1024**2) or
                    any(row['hard_bytes'] != 8*1024**2 for row in usage)):
                raise RuntimeError(f'kernel project usage is outside the expected bound: {usage}')
            print(json.dumps(dict(result='pass', filesystem='ext4+prjquota', capability=capability,
                overflow='EDQUOT', project_usage_bytes=project_usage), separators=(',', ':')))
        finally:
            environment_quota.CONFIG = old_config
    finally:
        if mounted:
            run('umount', str(mount))
        shutil.rmtree(scratch)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', default='ubuntu')
    control(parser.parse_args().owner)


if __name__ == '__main__':
    main()
