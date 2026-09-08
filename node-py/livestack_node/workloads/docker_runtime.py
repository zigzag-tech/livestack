"""Bounded, deterministic RootlessKit control state outside long source paths."""
import json
import os
from pathlib import Path
import shutil
import sys

from .model import WorkloadError


def runtime_path(unit):
    # Unit identity was validated by SystemdExecutor.unit. Keep Unix sockets
    # below Linux's 104-byte path limit regardless of the workspace prefix.
    import hashlib
    return Path('/run/user')/str(os.getuid())/('hw-'+hashlib.sha256(unit.encode()).hexdigest()[:24])


def prepare(unit, argv, cwd, output):
    path = runtime_path(unit)
    path.mkdir(mode=0o700, exist_ok=False)
    (path/'owner.json').write_text(json.dumps({'unit': unit}))
    inner = Path(output)/'docker-execution.json'
    inner.write_text(json.dumps(dict(unit=unit, argv=argv, cwd=str(cwd), output=str(output))))
    inner.chmod(0o600)
    return ['/usr/bin/rootlesskit', '--state-dir='+str(path), '--net=slirp4netns',
        '--disable-host-loopback', '--port-driver=builtin', '--copy-up=/etc', '--copy-up=/run',
        sys.executable, str(Path(__file__).with_name('docker_command.py').resolve()), str(inner)]


def cleanup(unit):
    path = runtime_path(unit)
    if not path.exists():
        return
    marker = path/'owner.json'
    if path.is_symlink() or path.stat().st_uid != os.getuid() or not marker.is_file() or marker.stat().st_size > 1024:
        raise WorkloadError('unrecognized Docker runtime; cleanup refused', 503)
    if json.loads(marker.read_text()) != {'unit': unit}:
        raise WorkloadError('Docker runtime owner mismatch', 503)
    shutil.rmtree(path)


def remove_data(root):
    """Delete subordinate-UID layers only after the attempt cgroup is stopped.

    The controller retains the cleanup claim until this finite operation ends.
    Production controllers themselves run in a bounded systemd service.
    """
    import subprocess
    data = Path(root)/'docker-data'
    if not data.exists():
        return
    if data.is_symlink() or data.resolve() != data:
        raise WorkloadError('Docker data is not in the private attempt tree', 503)
    reply = subprocess.run(['/usr/bin/rootlesskit', '/usr/bin/rm', '-rf', '--', str(data)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if reply.returncode or data.exists():
        raise WorkloadError('Docker layer cleanup failed; capacity remains reserved', 503)
