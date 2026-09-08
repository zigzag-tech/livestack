"""Installed private rootless Docker launcher, inside the attempt's namespace.

The Docker daemon, registered handler and containers share one delegated job
cgroup. The host Docker socket is never selected. Output goes to the existing
bounded execution writer; image and writable layers stay on the job filesystem.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def run(config_path):
    config = json.loads(Path(config_path).read_text())
    if os.getuid() != 0 or not os.environ.get('ROOTLESSKIT_STATE_DIR'):
        raise RuntimeError('Docker handler requires the installed rootless namespace')
    group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
    expected = '/' + config['unit'] + '/supervisor'
    if not group.endswith(expected):
        raise RuntimeError('Docker handler is outside its delegated attempt cgroup')
    parent = group.removesuffix('/supervisor')
    # RootlessKit copied /run into this mount namespace. Remove only its
    # convenience symlinks, as upstream dockerd-rootless.sh does, so Docker
    # cannot resolve plugin/runtime paths into the host daemon's directories.
    for name in ('/run/docker', '/run/docker.sock', '/run/containerd', '/run/xtables.lock'):
        path = Path(name)
        if path.is_symlink():
            path.unlink()
    runtime = Path('/run/harmony')
    runtime.mkdir(mode=0o700)
    data = Path(config['output']).parent/'docker-data'
    data.mkdir(mode=0o700)
    (runtime/'data').mkdir()
    subprocess.run(['/usr/bin/mount', '--bind', str(data), str(runtime/'data')], check=True)
    daemon_config = runtime/'daemon.json'
    daemon_config.write_text(json.dumps({'features': {'containerd-snapshotter': False},
        'log-driver': 'local', 'log-opts': {'max-size': '4m', 'max-file': '2'}}))
    client_config = Path(config['output']).parent/'docker-client'
    client_config.mkdir(mode=0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), DOCKER_CONFIG=str(client_config), DOCKER_HOST='unix:///run/harmony/docker.sock',
               _DOCKERD_ROOTLESS_CHILD='1')
    env['PATH'] = '/usr/local/sbin:/usr/sbin:/sbin:' + env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    # Do not let an inherited CLI context override the job's private socket.
    for key in ('DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'BUILDX_CONFIG', 'BUILDX_BUILDER', 'BUILDKIT_HOST'):
        env.pop(key, None)
    command = ['/usr/bin/dockerd', '--config-file='+str(daemon_config), '--rootless',
        '--data-root=/run/harmony/data', '--exec-root=/run/harmony/exec',
        '--pidfile=/run/harmony/docker.pid', '--host='+env['DOCKER_HOST'],
        '--exec-opt=native.cgroupdriver=cgroupfs', '--cgroup-parent='+parent,
        '--storage-driver=overlay2', '--shutdown-timeout=3']
    daemon = subprocess.Popen(command, env=env)
    try:
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise RuntimeError('private Docker daemon exited during preparation')
            try:
                ready = subprocess.run(['/usr/bin/docker', 'info', '--format', '{{.DockerRootDir}}'],
                    env=env, capture_output=True, text=True, timeout=3)
                if ready.returncode == 0 and ready.stdout.strip() == '/run/harmony/data':
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError('private Docker readiness deadline expired')
        return subprocess.call(config['argv'], cwd=config['cwd'], env=env)
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


if __name__ == '__main__':
    try:
        code = run(sys.argv[1])
    except Exception as error:
        print('Docker preparation failed: '+str(error), file=sys.stderr, flush=True)
        code = 75
    raise SystemExit(code)
