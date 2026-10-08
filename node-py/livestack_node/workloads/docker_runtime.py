"""Bounded, deterministic RootlessKit control state outside long source paths."""
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import sys

from .model import WorkloadError
from .docker_host_route import local_address


class RuntimeCleanupRefused(WorkloadError):
    """The runtime directory of an already-stopped unit cannot be removed now.

    Distinct type so the worker can keep claiming: the unit is gone (cleanup only
    runs after the cgroup is verified stopped) and attempt units are unique, so a
    leftover directory leaks a few socket files but endangers nothing.
    """


def runtime_base():
    # LIVESTACK_WORKLOAD_RUNTIME_BASE exists ONLY so tests can use a temp dir in
    # place of /run/user/<uid>; production never sets it.
    return Path(os.environ.get('LIVESTACK_WORKLOAD_RUNTIME_BASE') or Path('/run/user')/str(os.getuid()))


def runtime_path(unit):
    # Unit identity was validated by SystemdExecutor.unit. Keep Unix sockets
    # below Linux's 104-byte path limit regardless of the workspace prefix.
    import hashlib
    return runtime_base()/('hw-'+hashlib.sha256(unit.encode()).hexdigest()[:24])


def prepare(unit, argv, cwd, output, *, native_client=False, native_host_address=None, docker_cache=None):
    route = local_address(native_host_address) if native_client else None
    path = runtime_path(unit)
    # exist_ok=False: a leftover of the same unit is never silently reused. Unit
    # names are per attempt, and stop() removes any leftover before a retry.
    path.mkdir(mode=0o700, exist_ok=False)
    try:
        # Marker first, atomically, before rootlesskit or anything else exists
        # in the directory.
        tmp = path/'.owner.json.tmp'
        tmp.write_text(json.dumps({'unit': unit}))
        os.replace(tmp, path/'owner.json')
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    inner = Path(output)/'docker-execution.json'
    inner.write_text(json.dumps(dict(unit=unit, argv=argv, cwd=str(cwd), output=str(output),
                                    native_client=native_client, native_host_address=route, docker_cache=docker_cache)))
    inner.chmod(0o600)
    namespace = ['/usr/bin/rootlesskit', '--state-dir='+str(path), '--net=slirp4netns',
        '--disable-host-loopback', '--port-driver=builtin', '--copy-up=/etc', '--copy-up=/run',
        sys.executable, str(Path(__file__).with_name('docker_command.py').resolve()), str(inner)]
    if not native_client:
        return namespace
    frontend = Path(output)/'docker-native-execution.json'
    frontend.write_text(json.dumps(dict(unit=unit, namespace=namespace, argv=argv, cwd=str(cwd), output=str(output))))
    frontend.chmod(0o600)
    return [sys.executable, str(Path(__file__).with_name('docker_native.py').resolve()), str(frontend)]


def _users_of(path):
    """PIDs whose cwd/root/exe/open fds lie in `path`, or {0} if a socket is bound there.

    Bounded by the process table. Processes of other uids are unreadable and
    skipped: everything the worker's rootless Docker runs is the worker's uid.
    """
    prefix = str(path)
    def inside(target):
        return target == prefix or target.startswith(prefix+'/')
    found = set()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        links = [proc/'cwd', proc/'root', proc/'exe']
        try:
            links += list((proc/'fd').iterdir())
        except OSError:
            pass
        for link in links:
            try:
                if inside(os.readlink(link)):
                    found.add(int(proc.name))
                    break
            except OSError:
                continue
    try:
        # A listener's fd reads "socket:[inode]", so also look at bound paths.
        if any(prefix+'/' in line for line in Path('/proc/net/unix').read_text().splitlines()):
            found.add(0)
    except OSError:
        pass
    return found


def cleanup(unit):
    path = runtime_path(unit)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeCleanupRefused('unrecognized Docker runtime; cleanup refused', 503)
    # The directory name is hw-sha256(unit) and lives directly under the worker's
    # own runtime dir, so a readable marker naming ANOTHER unit is the only thing
    # that disproves ownership; a missing/unreadable marker means the attempt
    # died before (or while) writing it.
    owner = None
    try:
        marker = path/'owner.json'
        if marker.is_file() and not marker.is_symlink() and marker.stat().st_size <= 1024:
            owner = json.loads(marker.read_text())
    except (OSError, ValueError):
        owner = None
    if owner is not None and owner != {'unit': unit}:
        raise RuntimeCleanupRefused('Docker runtime owner mismatch', 503)
    users = _users_of(path)
    if users:
        raise RuntimeCleanupRefused('Docker runtime in use by %s; cleanup refused' %
            ('a bound socket' if users == {0} else 'pid '+','.join(str(p) for p in sorted(users - {0}))), 503)
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise RuntimeCleanupRefused('Docker runtime removal failed: %s: %s' % (type(error).__name__, error), 503)
    if owner is None:
        logging.warning('docker runtime cleanup: removed %s (no owner.json: attempt died before writing it)', path)


def run_in_userns(argv, timeout=60):
    """Run `argv` as uid 0 of the worker's subordinate-uid user namespace (RootlessKit).

    Needed to delete files rootless dockerd created as sub-uids. Returns
    (returncode, last 1 KiB of stderr). Raises WorkloadError (503) on a timeout
    or an unsafe user runtime directory.
    """
    import subprocess
    import resource
    import tempfile
    # Diagnostics have an active kernel byte bound, no named file/history, and
    # the same finite command deadline as cleanup. Never hide the real refusal.
    # Nested attempt TMPDIR paths exceed AF_UNIX's pathname limit. Keep the
    # rootless helper's finite state in the user-owned, kernel-bounded runtime
    # filesystem instead; never shorten or relocate the actual layer tree.
    runtime = Path('/run/user')/str(os.getuid())
    metadata = runtime.lstat()
    if not runtime.is_dir() or runtime.is_symlink() or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise WorkloadError('Docker cleanup user runtime directory is unsafe; capacity remains reserved',503)
    with tempfile.TemporaryDirectory(prefix='hcleanup-',dir=runtime) as state, tempfile.TemporaryFile() as diagnostic:
        try:
            reply = subprocess.run(['/usr/bin/rootlesskit', '--state-dir='+state, *argv],
                stdout=subprocess.DEVNULL, stderr=diagnostic, timeout=timeout,
                preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_FSIZE,(16384,16384)))
        except subprocess.TimeoutExpired as error:
            diagnostic.seek(max(0,diagnostic.tell()-1024))
            detail=diagnostic.read(1024).decode(errors='replace')
            raise WorkloadError('Docker layer cleanup timed out; capacity remains reserved: '+detail,503) from error
        diagnostic.seek(0,os.SEEK_END)
        diagnostic.seek(max(0,diagnostic.tell()-1024))
        detail=diagnostic.read(1024).decode(errors='replace')
    return reply.returncode, detail


def remove_data(root):
    """Delete subordinate-UID layers only after the attempt cgroup is stopped.

    The controller retains the cleanup claim until this finite operation ends.
    Production controllers themselves run in a bounded systemd service.
    Only the attempt-private `docker-data` is removed; a persistent docker_cache
    root lives outside the workspace and is never named here.
    """
    data = Path(root)/'docker-data'
    if not data.exists():
        return
    if data.is_symlink() or data.resolve() != data:
        raise WorkloadError('Docker data is not in the private attempt tree', 503)
    returncode, detail = run_in_userns(['/usr/bin/rm', '-rf', '--', str(data)])
    if returncode or data.exists():
        raise WorkloadError('Docker layer cleanup failed; capacity remains reserved; exit='+
                           str(returncode)+': '+detail,503)
    logging.info('Docker layer cleanup completed: %s',data)
