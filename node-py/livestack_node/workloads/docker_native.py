"""Native compilation frontend with a supervised private rootless Docker API.

The frontend stays in the host user namespace so it can authenticate the root
launch verifier. Only the Docker daemon/container descendants enter RootlessKit.
Both remain beneath the same attempt cgroup and reservation.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def identity(pid):
    root = Path('/proc')/str(pid)
    start = (root/'stat').read_text().rsplit(')', 1)[1].split()[19]
    group = (root/'cgroup').read_text().strip().split('::', 1)[1]
    return start, group


def run(config_path):
    config = json.loads(Path(config_path).read_text())
    group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
    if not group.endswith('/'+config['unit']+'/supervisor'):
        raise RuntimeError('native Docker frontend is outside delegated attempt containment')
    parent = group.removesuffix('/supervisor')
    ready = Path(config['output'])/'docker-native-ready.json'
    if ready.exists() or ready.is_symlink():
        raise RuntimeError('private Docker readiness record already exists')
    namespace = subprocess.Popen(config['namespace'])
    owned = None
    try:
        deadline = time.monotonic()+60
        while not ready.exists():
            if namespace.poll() is not None:
                raise RuntimeError('private Docker namespace stopped before readiness')
            if time.monotonic() >= deadline:
                raise RuntimeError('private Docker native readiness deadline expired')
            time.sleep(.1)
        fd = os.open(ready, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise RuntimeError('private Docker readiness record invalid')
        record = json.loads(raw)
        if (not isinstance(record, dict) or set(record) != {'pid', 'socket'} or
                type(record['pid']) is not int or record['pid'] <= 1 or
                record['socket'] != '/run/harmony/docker.sock'):
            raise RuntimeError('private Docker readiness identity invalid')
        pid = record['pid']
        owned = (pid, identity(pid))
        if not owned[1][1].startswith(parent+'/'):
            raise RuntimeError('private Docker daemon is outside attempt containment')
        env = dict(os.environ, DOCKER_HOST=f'unix:///proc/{pid}/root'+record['socket'],
                   DOCKER_CONFIG=str(Path(config['output']).parent/'docker-client'))
        for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'BUILDX_CONFIG',
                    'BUILDX_BUILDER', 'BUILDKIT_HOST'):
            env.pop(key, None)
        # Positive instrument: this exact endpoint must name our private data
        # root before any installed handler can ask it to build.
        probe = subprocess.run(['/usr/bin/docker', 'info', '--format', '{{.DockerRootDir}}'],
            env=env, capture_output=True, text=True, timeout=5, check=True)
        if probe.stdout.strip() != '/run/harmony/data':
            raise RuntimeError('private Docker native endpoint identity mismatch')
        return subprocess.call(config['argv'], cwd=config['cwd'], env=env)
    finally:
        if owned is not None:
            pid, expected = owned
            try:
                if identity(pid) == expected:
                    os.kill(pid, signal.SIGTERM)
            except FileNotFoundError:
                pass
        try:
            namespace.wait(timeout=5)
        except subprocess.TimeoutExpired:
            namespace.terminate()
            try:
                namespace.wait(timeout=5)
            except subprocess.TimeoutExpired:
                namespace.kill()
                namespace.wait(timeout=5)


if __name__ == '__main__':
    try:
        code = run(sys.argv[1])
    except Exception as error:
        print('Private Docker native frontend failed: '+str(error), file=sys.stderr, flush=True)
        code = 75
    raise SystemExit(code)
