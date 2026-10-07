"""Root-owned worker-local verifier; Unix peers must inhabit the live attempt.

Linux: the attempt is the systemd unit's cgroup. macOS: the attempt is the
launchd job's process tree; the root PID and the enforced limits come from
launchd itself (openspec/changes/apple-host-compilation). Windows: a LocalSystem
service on a named pipe; the attempt is its named Job Object, membership and
limits come from the kernel (openspec/changes/windows-host-worker).

Run as an installed service with a root-owned config. Worker credentials never
enter the handler environment. The worker still owns supervision/cleanup; this
service authenticates a launch on behalf of one configured worker slot.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

from livestack_node import transport
from .launch_contract import MAX_BYTES, DEADLINE_SECONDS, receive, remaining, trusted_json, validate_request
from .model import WorkloadError, encode, name

DARWIN = sys.platform == 'darwin'
WINDOWS = sys.platform == 'win32'
if DARWIN:
    from . import darwin_proc
    from .darwin_supervision import job_label, launchd_job
if WINDOWS:
    import threading
    from . import windows_proc
    from .windows_pipe import PipeListener
    from .windows_supervision import attempt_job
else:
    import pwd


@contextmanager
def launch_deadline():
    if WINDOWS:
        # No SIGALRM: every pipe read/write and the authority request is
        # individually bounded by the remaining deadline instead.
        yield time.monotonic()+DEADLINE_SECONDS
        return

    def expired(*_):
        raise WorkloadError('compilation_verification_deadline', 503)
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        yield time.monotonic()+DEADLINE_SECONDS
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def bounded_json(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise WorkloadError('compilation_worker_journal_invalid', 403)
        raw = source.read(65537)
    if len(raw) > 65536:
        raise WorkloadError('compilation_worker_journal_oversized', 403)
    return json.loads(raw)


def machine_identity():
    """32 lowercase hex: /etc/machine-id, or IOPlatformUUID without dashes."""
    if DARWIN:
        return darwin_proc.platform_uuid()
    if WINDOWS:
        return windows_proc.machine_guid()
    return Path('/etc/machine-id').read_text().strip()


def darwin_attempt(config, attempt):
    """(pid, start, limits) of the attempt's launchd job, from launchd."""
    job = launchd_job('gui/'+str(config['worker_uid']), job_label(config['worker'], attempt), timeout=1)
    if job is None or job['state'] != 'running' or job['pid'] is None:
        raise WorkloadError('compilation_attempt_containment_unavailable', 403)
    record = darwin_proc.info(job['pid'])
    if record is None or record['uid'] != config['worker_uid']:
        raise WorkloadError('compilation_attempt_containment_unavailable', 403)
    arguments, limits = job['arguments'], {}
    for flag in ('--memory-bytes', '--cpu', '--tasks', '--max-seconds'):
        if arguments.count(flag) != 1 or arguments.index(flag)+1 >= len(arguments):
            raise WorkloadError('compilation_attempt_resource_limit_missing', 403)
        limits[flag] = float(arguments[arguments.index(flag)+1])
    return record['pid'], record['start'], limits


def windows_attempt(config, attempt, peer):
    """(job, kernel limits) of the attempt's Job Object, opened by the name the
    CONFIGURED worker and the attempt derive, after the kernel confirms the
    peer process is a member."""
    job = windows_proc.Job.open(attempt_job(config['worker'], attempt), windows_proc.JOB_OBJECT_QUERY)
    if job is None:
        raise WorkloadError('compilation_attempt_containment_unavailable', 403)
    try:
        if not job.contains(peer):
            raise WorkloadError('compilation_peer_outside_attempt', 403)
        limits = job.limits()
    finally:
        job.close()
    return limits


def process_identity(pid):
    # Field 22 starttime plus kernel peer PID closes PID-reuse races around the
    # authority request. Read /proc in the verifier's host PID/cgroup namespace.
    fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')', 1)[1].split()
    groups = (Path('/proc')/str(pid)/'cgroup').read_text()
    if len(groups.encode()) > MAX_BYTES:
        raise WorkloadError('compilation_peer_cgroup_oversized', 403)
    unified = [line[3:] for line in groups.splitlines() if line.startswith('0::')]
    if len(unified) != 1 or not unified[0].startswith('/'):
        raise WorkloadError('compilation_peer_cgroup_unknown', 403)
    return fields[19], unified[0]


def owned_group(config, attempt, deadline):
    unit = 'harmony-work-'+hashlib.sha256(config['worker'].encode()).hexdigest()[:16]+'-'+attempt+'.service'
    uid = config['worker_uid']
    account = pwd.getpwuid(uid).pw_name
    commands = [
        ['/usr/sbin/runuser', '-u', account, '--', '/usr/bin/env',
         'XDG_RUNTIME_DIR=/run/user/'+str(uid),
         'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(uid)+'/bus',
         '/usr/bin/systemctl', '--user', 'show', unit, '--property=ActiveState,ControlGroup'],
        ['/usr/bin/systemctl', '--system', 'show', unit, '--property=ActiveState,ControlGroup'],
    ]
    active_groups = []
    for command in commands:
        result = subprocess.run(command, check=False, capture_output=True, text=True,
                                timeout=min(1, remaining(deadline)))
        if result.returncode:
            continue
        if len(result.stdout.encode()) > MAX_BYTES:
            raise WorkloadError('compilation_unit_report_oversized', 503)
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        group = state.get('ControlGroup', '')
        if state.get('ActiveState') == 'active':
            if not group.startswith('/') or group == '/':
                raise WorkloadError('compilation_attempt_containment_unavailable', 403)
            active_groups.append(group)
    if len(active_groups) != 1:
        raise WorkloadError('compilation_attempt_containment_unavailable', 403)
    return active_groups[0]


def authority_receipt(config, request, deadline):
    target, path = transport.split_target(config['authority'].rstrip('/')+'/v1/workloads/worker/verify-compilation')
    body = dict(boot=request['boot'], attempt_id=request['attempt_id'], fence=request['fence'],
                input_digest=request['input_digest'], **{'class': request['class']})
    while True:
        try:
            with transport.dial_stream(target, 'POST', path,
                    headers={'Authorization': 'Bearer '+config['token'], 'Content-Type': 'application/json'},
                    body=encode(body).encode(), timeout=min(2, remaining(deadline))) as response:
                raw = response.read(MAX_BYTES+1)
            break
        except transport.HTTPError as error:
            raw = error.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                raise WorkloadError('compilation_authority_response_oversized', 503)
            value = json.loads(raw)
            reason = value.get('error') if isinstance(value, dict) else None
            if not isinstance(reason, str) or not reason:
                raise WorkloadError('compilation_authority_refusal_missing_reason', 503)
            raise WorkloadError(reason[:1024], error.code) from error
        except OSError as error:
            # verify-compilation is a read, so a lost connection is retried
            # inside this launch's wall deadline; an authority answer never is.
            # One try over xc-win-1-wsl's 230 ms / ~10%-loss path refused
            # launches mid-attempt (2026-10-02, docs/harmony-worker-enrolment.md).
            logging.warning('compilation_authority_transport_retry: failure=%s %s',
                            type(error).__name__, str(error)[:200])
            time.sleep(min(.2, remaining(deadline)))
    if len(raw) > MAX_BYTES:
        raise WorkloadError('compilation_authority_response_oversized', 503)
    return json.loads(raw)


def verify_resource_caps(group, receipt):
    resources = receipt.get('execution_resources')
    if not isinstance(resources, dict):
        raise WorkloadError('compilation_launch_contract_unsupported', 403)
    if DARWIN:
        # The wrapper enforces what launchd started it with (no cgroup exists).
        limits = group[2]
        memory, cpu = limits['--memory-bytes'], limits['--cpu']
    elif WINDOWS:
        # The kernel's job limits; CpuRate is 1/100 % of the whole machine.
        if group['memory_bytes'] is None or group['cpu_rate'] is None:
            raise WorkloadError('compilation_attempt_resource_limit_missing', 403)
        memory, cpu = group['memory_bytes'], group['cpu_rate']/10000*(os.cpu_count() or 1)-1e-9
    else:
        root = Path('/sys/fs/cgroup')/group.lstrip('/')
        memory = (root/'memory.max').read_text().strip()
        quota, period = (root/'cpu.max').read_text().split()
        if memory == 'max' or quota == 'max':
            raise WorkloadError('compilation_attempt_resource_limit_missing', 403)
        memory, cpu = int(memory), int(quota)/int(period)
    memory_cap, cpu_cap = resources.get('memory_bytes'), resources.get('cpu')
    if (type(memory_cap) not in (int, float) or type(cpu_cap) not in (int, float) or
            not memory_cap > 0 or not cpu_cap > 0 or
            memory > memory_cap or cpu > cpu_cap):
        raise WorkloadError('compilation_attempt_resource_limit_mismatch', 403)


def verify_peer(config, request, pid, uid, deadline):
    validate_request(request)
    if request['worker'] != config['worker'] or uid != config['worker_sid' if WINDOWS else 'worker_uid']:
        raise WorkloadError('compilation_peer_worker_mismatch', 403)
    if request['host'] != config['host']:
        raise WorkloadError('compilation_peer_physical_host_mismatch', 403)
    attempt = request['attempt_id']
    if len(attempt) != 32 or any(c not in '0123456789abcdef' for c in attempt):
        raise WorkloadError('compilation_attempt_identity_invalid', 403)
    journal = bounded_json(config['journal'])
    if not isinstance(journal, dict):
        raise WorkloadError('compilation_worker_journal_invalid', 403)
    assignment = journal.get('assignment')
    if journal.get('phase') != 'running' or not isinstance(assignment, dict):
        raise WorkloadError('compilation_worker_not_executing', 403)
    compilation = assignment.get('compilation')
    if (not isinstance(compilation, dict) or type(compilation.get('version')) is not int or
            compilation['version'] != 1):
        raise WorkloadError('compilation_not_admitted: no worker grant', 403)
    expected = dict(worker=assignment['worker'], boot=assignment['boot'],
                    job_id=assignment['job_id'], attempt_id=assignment['attempt_id'],
                    fence=assignment['fence'], input_digest=assignment['spec']['input_digest'],
                    host=compilation['host'], policy_revision=compilation['policy_revision'])
    if any(request[key] != value for key, value in expected.items()):
        raise WorkloadError('compilation_worker_assignment_mismatch', 403)
    if WINDOWS:
        # `pid` is an open process handle: held from accept, it pins the
        # process, so a recycled PID cannot stand in for it later.
        group = windows_attempt(config, attempt, pid)
        identity = None
    elif DARWIN:
        group = darwin_attempt(config, attempt)
        identity = darwin_proc.info(pid)
        if identity is None or not darwin_proc.descends_from(pid, group[0], group[1]):
            raise WorkloadError('compilation_peer_outside_attempt', 403)
    else:
        group = owned_group(config, attempt, deadline)
        identity = process_identity(pid)
        if identity[1] != group and not identity[1].startswith(group+'/'):
            raise WorkloadError('compilation_peer_outside_attempt', 403)
    receipt = authority_receipt(config, request, deadline)
    if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int or receipt['version'] != 1 or
            any(receipt.get(key) != value for key, value in expected.items()) or
            request['class'] not in receipt.get('classes', [])):
        raise WorkloadError('compilation_authority_receipt_mismatch', 403)
    verify_resource_caps(group, receipt)
    if WINDOWS:
        code = ctypes_exit_code(pid)
        changed = code != windows_proc.STILL_ACTIVE or windows_attempt(config, attempt, pid) != group
    elif DARWIN:
        changed = (darwin_proc.info(pid) != identity or darwin_attempt(config, attempt) != group or
                   not darwin_proc.descends_from(pid, group[0], group[1]))
    else:
        changed = process_identity(pid) != identity or owned_group(config, attempt, deadline) != group
    if changed:
        raise WorkloadError('compilation_peer_containment_changed', 403)
    remaining(deadline)
    return receipt


def ctypes_exit_code(handle):
    import ctypes
    from ctypes import wintypes
    code = wintypes.DWORD()
    if not windows_proc._k32().GetExitCodeProcess(handle, ctypes.byref(code)):
        return None
    return code.value


def answer(config, stream, peer, uid, label):
    """One request on any transport: verify, reply, log; never raises."""
    request = None
    try:
        with launch_deadline() as deadline:
            request = receive(stream, deadline)
            receipt = verify_peer(config, request, peer, uid, deadline)
            reply = dict(version=1, ok=True, receipt=receipt)
        logging.info('compilation_launch_admitted: worker=%s attempt=%s class=%s peer=%s',
                     request['worker'], request['attempt_id'], request['class'], label)
    except WorkloadError as error:
        reply = dict(version=1, ok=False, error=str(error)[:1024])
        logging.warning('compilation_launch_refused: peer=%s reason=%s', label, str(error)[:1024])
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        reply = dict(version=1, ok=False, error='compilation_verification_unavailable')
        logging.warning('compilation_launch_refused: peer=%s failure=%s: %s', label, type(error).__name__,
                        str(error)[:256])
    stream.settimeout(.1 if not WINDOWS else 1)
    try:
        stream.sendall(encode(reply, MAX_BYTES-1).encode()+b'\n')
    except (OSError, WorkloadError) as error:
        logging.warning('compilation_launch_reply_failed: peer=%s failure=%s', label, type(error).__name__)


class WindowsLaunchServer:
    """The verifier on Windows: must run as LocalSystem. One pipe name per
    worker slot; each connection is answered on its own thread, at most
    16 at once (the Unix server's listen backlog)."""

    def __init__(self, config):
        if windows_proc.current_user_sid() != windows_proc.SYSTEM_SID:
            raise WorkloadError('compilation_verifier_requires_root_service', 403)
        required = {'version', 'worker', 'worker_sid', 'host', 'machine_id', 'authority', 'token', 'journal', 'pipe'}
        if (not isinstance(config, dict) or set(config) != required or type(config['version']) is not int or
                config['version'] != 1 or not isinstance(config['worker_sid'], str) or
                not config['worker_sid'].startswith('S-1-5-21-')):
            raise WorkloadError('compilation_verifier_config_invalid', 403)
        validate_common(config)
        from .windows_pipe import PIPE_PREFIX
        if not isinstance(config['pipe'], str) or not config['pipe'].startswith(PIPE_PREFIX):
            raise WorkloadError('compilation_verifier_socket_invalid', 403)
        self.config = config
        self.listener = PipeListener(config['pipe'], config['worker_sid'])
        self.slots = threading.BoundedSemaphore(16)

    def _serve_one(self, stream, pid):
        peer = windows_proc.open_process(pid)
        try:
            uid = windows_proc.process_user_sid(peer) if peer else None
            if peer is None:
                raise OSError('peer process vanished')
            answer(self.config, stream, peer, uid, f'pid {pid} sid {uid}')
        except OSError as error:
            logging.warning('compilation_launch_refused: peer=pid %s failure=%s', pid, error)
        finally:
            windows_proc.close_handle(peer)
            stream.close()
            self.slots.release()

    def serve_forever(self):
        while True:
            self.slots.acquire()
            try:
                stream, pid = self.listener.accept()
            except BaseException:
                self.slots.release()
                raise
            threading.Thread(target=self._serve_one, args=(stream, pid), daemon=True).start()


class LaunchHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request = None
        if DARWIN:
            pid, uid = darwin_proc.peer_credentials(self.request)
        else:
            pid, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            with launch_deadline() as deadline:
                request = receive(self.request, deadline)
                receipt = verify_peer(self.server.config, request, pid, uid, deadline)
                reply = dict(version=1, ok=True, receipt=receipt)
            logging.info('compilation_launch_admitted: worker=%s attempt=%s class=%s peer_uid=%s',
                         request['worker'], request['attempt_id'], request['class'], uid)
        except WorkloadError as error:
            reply = dict(version=1, ok=False, error=str(error)[:1024])
            logging.warning('compilation_launch_refused: peer_uid=%s reason=%s', uid, str(error)[:1024])
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
            reply = dict(version=1, ok=False, error='compilation_verification_unavailable')
            logging.warning('compilation_launch_refused: peer_uid=%s failure=%s: %s', uid, type(error).__name__,
                            str(error)[:256])
        self.request.settimeout(.1)
        try:
            self.request.sendall(encode(reply, MAX_BYTES-1).encode()+b'\n')
        except OSError as error:
            logging.warning('compilation_launch_reply_failed: peer_uid=%s failure=%s', uid, type(error).__name__)


def validate_common(config):
    name(config['worker'], 'worker')
    name(config['host'], 'physical host')
    if (not isinstance(config['machine_id'], str) or len(config['machine_id']) != 32 or
            any(c not in '0123456789abcdef' for c in config['machine_id']) or
            machine_identity() != config['machine_id']):
        raise WorkloadError('compilation_verifier_physical_machine_mismatch', 403)
    if not isinstance(config['token'], str) or len(config['token']) < 32:
        raise WorkloadError('compilation_verifier_credential_invalid', 403)
    # Numerical authority endpoints avoid an unbounded resolver operation;
    # every request still has an absolute wall deadline.
    endpoint = urlsplit(config['authority'])
    if endpoint.scheme not in ('http', 'https') or endpoint.username or endpoint.password:
        raise WorkloadError('compilation_verifier_authority_invalid', 403)
    ipaddress.ip_address(endpoint.hostname)


# Windows Python has no AF_UNIX server; WindowsLaunchServer serves there.
class LaunchServer(getattr(socketserver, 'UnixStreamServer', object)):
    request_queue_size = 16

    def __init__(self, config):
        if os.geteuid() != 0:
            raise WorkloadError('compilation_verifier_requires_root_service', 403)
        required = {'version', 'worker', 'worker_uid', 'host', 'machine_id',
                    'authority', 'token', 'journal', 'socket'}
        if (not isinstance(config, dict) or set(config) != required or type(config['version']) is not int or
                config['version'] != 1 or type(config['worker_uid']) is not int or config['worker_uid'] <= 0):
            raise WorkloadError('compilation_verifier_config_invalid', 403)
        validate_common(config)
        socket_path = Path(config['socket'])
        if not socket_path.is_absolute() or len(str(socket_path).encode()) > 103:
            raise WorkloadError('compilation_verifier_socket_invalid', 403)
        # Registry trust also requires this directory to be root controlled.
        for directory in socket_path.parents:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise WorkloadError('compilation_verifier_socket_directory_untrusted', 403)
        self.config = config
        super().__init__(str(socket_path), LaunchHandler)
        socket_path.chmod(0o666)


def main_windows(args):
    """As a Windows service (LocalSystem); --console for diagnosis."""
    from .windows_service import Service

    def run():
        # The log first: a refusal to start must name its reason somewhere.
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
            RotatingFileHandler(Path(args.config).parent/'verifier.log', maxBytes=2*1024**2, backupCount=2)])
        config = trusted_json(args.config, secret=True)
        server = WindowsLaunchServer(config)
        logging.info('compilation verifier listening on %s for %s', config['pipe'], config['worker'])
        server.serve_forever()
    if args.console:
        run()
    else:
        Service(args.service_name, None, target=run).dispatch()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--service-name', default='LivestackCompilationVerifier')
    parser.add_argument('--console', action='store_true')
    args = parser.parse_args()
    if WINDOWS:
        main_windows(args)
        return
    config = trusted_json(args.config, secret=True)
    with LaunchServer(config) as server:
        # The file lives in the RuntimeDirectory, which a reboot (or `wsl
        # --shutdown`) erases; stderr reaches the journal, which keeps the
        # refusals of earlier boots (2026-10-02 WSL triage found none).
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
            RotatingFileHandler(Path(config['socket']).parent/'verifier.log', maxBytes=2*1024**2, backupCount=2),
            logging.StreamHandler()])
        try:
            server.serve_forever(poll_interval=.2)
        finally:
            Path(config['socket']).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
