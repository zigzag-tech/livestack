"""Trusted local compilation verification transport; metadata is not a grant."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time

from .compilation_policy import CLASSES, VERSION
from .model import WorkloadError, encode, name


MAX_BYTES = 16384
DEADLINE_SECONDS = 5
REGISTRY = '/etc/livestack/compilation-launch.json'
REQUEST_FIELDS = frozenset({'version', 'worker', 'host', 'policy_revision', 'boot',
                           'job_id', 'attempt_id', 'fence', 'input_digest', 'class'})


def trusted_json(path, *, secret=False):
    """Root-owned metadata under directories a caller cannot replace."""
    path = Path(path)
    if not path.is_absolute():
        raise WorkloadError('compilation_registry_untrusted', 403)
    for directory in path.parents:
        info = directory.lstat()
        writable = info.st_mode & 0o022
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or (writable and not sticky_root):
            raise WorkloadError('compilation_registry_untrusted', 403)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_mode & (0o077 if secret else 0o022) or info.st_size > MAX_BYTES):
            raise WorkloadError('compilation_registry_untrusted', 403)
        raw = source.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise WorkloadError('compilation_registry_oversized', 403)
    return json.loads(raw)


def remaining(deadline):
    left = deadline-time.monotonic()
    if left <= 0:
        raise WorkloadError('compilation_verification_deadline', 503)
    return left


def receive(connection, deadline):
    data = bytearray()
    while b'\n' not in data:
        connection.settimeout(remaining(deadline))
        chunk = connection.recv(min(4096, MAX_BYTES+1-len(data)))
        if not chunk:
            raise WorkloadError('compilation_verifier_incomplete_response', 503)
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise WorkloadError('compilation_verifier_message_oversized', 413)
    line, tail = data.split(b'\n', 1)
    if tail:
        raise WorkloadError('compilation_verifier_extra_message', 400)
    return json.loads(line)


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != REQUEST_FIELDS or
            type(request['version']) is not int or request['version'] != VERSION):
        raise WorkloadError('compilation_launch_contract_unsupported', 403)
    for field in ('worker', 'host', 'policy_revision', 'boot', 'job_id', 'attempt_id'):
        name(request[field], field)
    if (type(request['fence']) is not int or request['fence'] < 1 or
            not isinstance(request['class'], str) or request['class'] not in CLASSES):
        raise WorkloadError('compilation_launch_identity_invalid', 403)
    digest = request['input_digest']
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise WorkloadError('compilation_launch_input_invalid', 403)


def verify_launch(request, *, registry_path=REGISTRY):
    """No registry/socket override from caller environment can mint a grant.

    The alternate registry path is for root-installed deployments/disposable
    integration fixtures; it receives exactly the same root ownership checks.
    """
    validate_request(request)
    if sys.platform != 'linux':
        raise WorkloadError('compilation_launch_platform_unsupported', 403)
    deadline = time.monotonic()+DEADLINE_SECONDS
    try:
        registry = trusted_json(registry_path)
        if (not isinstance(registry, dict) or set(registry) != {'version', 'slots'} or
                type(registry['version']) is not int or registry['version'] != VERSION or
                not isinstance(registry['slots'], dict) or not 1 <= len(registry['slots']) <= 128):
            raise WorkloadError('compilation_launch_contract_unsupported', 403)
        endpoint = registry['slots'].get(request['worker'])
        if (not isinstance(endpoint, str) or not endpoint.startswith('/') or
                len(endpoint.encode()) > 103):
            raise WorkloadError('compilation_verifier_slot_unavailable', 503)
        # Socket ownership alone is not proof: SO_PEERCRED authenticates the
        # connected server, including a raced replacement/forged socket path.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining(deadline))
            connection.connect(endpoint)
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != 0:
                raise WorkloadError('compilation_verifier_peer_untrusted', 403)
            connection.sendall(encode(request, MAX_BYTES-1).encode()+b'\n')
            response = receive(connection, deadline)
        remaining(deadline)
        if (not isinstance(response, dict) or response.get('version') != VERSION or
                type(response.get('version')) is not int or type(response.get('ok')) is not bool):
            raise WorkloadError('compilation_launch_contract_unsupported', 403)
        if not response['ok']:
            reason = response.get('error')
            if not isinstance(reason, str) or not reason:
                raise WorkloadError('compilation_verifier_refusal_missing_reason', 503)
            raise WorkloadError(reason[:1024], 403)
        receipt = response.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('version') != VERSION:
            raise WorkloadError('compilation_launch_contract_unsupported', 403)
        for field in ('worker', 'host', 'policy_revision', 'boot', 'job_id', 'attempt_id', 'fence', 'input_digest'):
            if receipt.get(field) != request[field]:
                raise WorkloadError('compilation_verifier_receipt_mismatch', 403)
        if request['class'] not in receipt.get('classes', []):
            raise WorkloadError('compilation_class_not_reserved', 403)
        return receipt
    except WorkloadError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise WorkloadError('compilation_verification_unavailable', 503) from error


def environment_request(compilation_class, env=None):
    env = os.environ if env is None else env
    fields = dict(worker='HARMONY_WORKER', host='HARMONY_PHYSICAL_HOST',
                  policy_revision='HARMONY_POLICY_REVISION', boot='HARMONY_BOOT',
                  job_id='HARMONY_JOB', attempt_id='HARMONY_ATTEMPT', input_digest='HARMONY_INPUT_DIGEST')
    request = {key: env.get(variable) for key, variable in fields.items()}
    try:
        request.update(version=VERSION, fence=int(env.get('HARMONY_FENCE', '')), **{'class': compilation_class})
    except ValueError as error:
        raise WorkloadError('compilation_not_admitted: missing attempt metadata', 403) from error
    return request
