"""Bounded source/artifact transfer over the same authenticated control plane."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import time

from livestack_node import transport

from .archive import file_digest
from .blobs import BlobStore
from .model import ArtifactTooLarge, WorkloadError
from .download import download_into


class InputTransfer:
    """Object PUT/GET against the authority, optionally relay-first.

    `relay` is a client for a stateless edge forwarder (edge_forward.py) in front
    of the SAME authority: same credentials, same routes. It is tried first and
    skipped, with the reason logged, when it reports its budget exhausted or is
    unreachable; every failure falls back to the authority, never to nothing.
    """
    STATUS_TTL = 30

    def __init__(self, client, *, max_bytes=2*1024**3, relay=None, relay_key=None, relay_parallel=4):
        if (relay is None) != (relay_key is None):
            raise ValueError('relay and relay_key are configured together')
        self.client = client
        self.max_bytes = max_bytes
        self.relay = relay
        self.relay_key = relay_key
        self.relay_parallel = relay_parallel
        self._relay_checked = (0.0, None)

    def _relay_usable(self):
        """None when usable, else the named reason it is being skipped."""
        if self.relay is None:
            return 'not configured'
        checked, reason = self._relay_checked
        if checked and time.monotonic()-checked < self.STATUS_TTL:
            return reason
        try:
            target, path = transport.split_target(self.relay.url.replace('/v1/workloads/', '/v1/edge/status'))
            status, _headers, body = transport.dial(target, 'GET', path, headers={}, timeout=5)
            state = json.loads(body).get('state')
            reason = None if status == 200 and state == 'ok' else (state or 'status %s' % status)
        except (OSError, ValueError, WorkloadError) as error:
            reason = 'unreachable: %s' % error
        self._relay_checked = (time.monotonic(), reason)
        if reason:
            logging.warning('object relay skipped (%s); using the authority directly', reason)
        return reason

    def _headers(self, client, assignment):
        headers = self.headers(assignment)
        if client is self.relay:
            headers['X-Edge-Key'] = self.relay_key
        return headers

    def _clients(self):
        return [self.relay, self.client] if self._relay_usable() is None else [self.client]

    def headers(self, assignment=None):
        result = {'Authorization': 'Bearer '+self.client.token}
        if assignment is not None:
            result.update({'X-Workload-Attempt': assignment['attempt_id'],
                           'X-Workload-Fence': str(assignment['fence']),
                           'X-Workload-Boot': assignment['boot']})
        return result

    def put(self, source, *, assignment=None):
        source = Path(source)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise ArtifactTooLarge(size, self.max_bytes)
        digest = file_digest(source)
        clients, last = self._clients(), None
        for client in clients:
            headers = self._headers(client, assignment)
            headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(size)})
            try:
                with source.open('rb') as stream:
                    target, path = transport.split_target(client.url+'objects/'+digest)
                    _status, _headers, body = transport.dial(
                        target, 'PUT', path, headers=headers, body=stream,
                        timeout=client.timeout)
                    if len(body) > 65536:
                        raise WorkloadError('upload response exceeds bound', 502)
                    result = json.loads(body)
                    if result.get('digest') != digest or result.get('size') != size:
                        raise WorkloadError('upload acknowledgement mismatch', 502)
                    return result
            except (OSError, ValueError, WorkloadError) as error:
                if client is self.client:
                    raise
                last = error
                logging.warning('object relay upload failed (%s); retrying via the authority', error)
        raise last

    def get(self, digest, destination, *, assignment=None):
        digest = BlobStore.digest(digest)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if file_digest(destination) == digest:
                return destination
            raise WorkloadError('cached object has wrong digest', 409)
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                for client in self._clients():
                    out.seek(0)
                    out.truncate()
                    try:
                        # A relay attempt gets few retries: the authority is the fallback.
                        download_into(client, digest, self._headers(client, assignment), out, self.max_bytes,
                                      **({} if client is self.client else {'max_failures': 2, 'parallel': self.relay_parallel}))
                        break
                    except (OSError, WorkloadError) as error:
                        if client is self.client:
                            raise
                        logging.warning('object relay download failed (%s); retrying via the authority', error)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)
