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

# This installed entry point is invoked by filename with the handler's minimal
# environment. Resolve its own selected SDK rather than inherited PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from livestack_node.workloads import docker_cache
from livestack_node.workloads.docker_host_route import canonical_address


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
    (runtime/'data').mkdir()
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
    if config.get('native_client'):
        command.append('--host-gateway-ip='+canonical_address(config.get('native_host_address')))
    cache = docker_cache.Session(config.get('docker_cache'), Path(config['output']))
    daemon = None
    code = None
    try:
        persistent = cache.begin()
        starting = time.monotonic()
        for data in ([persistent, None] if persistent is not None else [None]):
            if data is None:
                data = Path(config['output']).parent/'docker-data'
                data.mkdir(mode=0o700)
            subprocess.run(['/usr/bin/mount', '--bind', str(data), str(runtime/'data')], check=True)
            daemon = subprocess.Popen(command, env=env)
            try:
                wait_ready(daemon, env)
                break
            except RuntimeError:
                stop(daemon)
                daemon = None
                subprocess.run(['/usr/bin/umount', '-l', str(runtime/'data')], check=True)
                if data is not persistent:
                    raise
                # Fail closed to cold: a root dockerd cannot start on is wiped, the attempt runs ephemeral.
                cache.start_failed()
        cache.timed('dockerd_start', starting)
        cache.announce()
        if config.get('native_client'):
            # No PID namespace was requested by the installed RootlessKit argv;
            # this PID is the host PID the native frontend authenticates.
            ready = Path(config['output'])/'docker-native-ready.json'
            temporary = ready.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': daemon.pid, 'socket': '/run/harmony/docker.sock'}))
            temporary.chmod(0o600)
            os.replace(temporary, ready)
            code = daemon.wait()
            return code
        code = subprocess.call(config['argv'], cwd=config['cwd'], env=env)
        if cache.root is not None:
            cache.result['prune'] = docker_cache.prune(env, cache.plan['max_bytes'])
            cache.phases['prune'] = cache.result['prune'].get('seconds', 0.0)
        return code
    finally:
        stopping = time.monotonic()
        if daemon is not None and not stop(daemon, 15 if cache.root is not None else 5):
            cache.unclean = True
        cache.timed('dockerd_stop', stopping)
        # In native mode `code` is dockerd's own status; the frontend recorded the handler's.
        cache.finish(None if config.get('native_client') else code)


def wait_ready(daemon, env):
    deadline = time.monotonic()+60
    while time.monotonic() < deadline:
        if daemon.poll() is not None:
            raise RuntimeError('private Docker daemon exited during preparation')
        try:
            ready = subprocess.run(['/usr/bin/docker', 'info', '--format', '{{.DockerRootDir}}'],
                env=env, capture_output=True, text=True, timeout=3)
            if ready.returncode == 0 and ready.stdout.strip() == '/run/harmony/data':
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(.2)
    raise RuntimeError('private Docker readiness deadline expired')


def stop(daemon, timeout=5):
    """Stop dockerd; False if it had to be killed (its store may then be inconsistent)."""
    daemon.terminate()
    try:
        daemon.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()
        return False


if __name__ == '__main__':
    try:
        code = run(sys.argv[1])
    except Exception as error:
        print('Docker preparation failed: '+str(error), file=sys.stderr, flush=True)
        code = 75
    raise SystemExit(code)
