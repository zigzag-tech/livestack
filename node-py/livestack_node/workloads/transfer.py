"""Bounded source/artifact transfer over the same authenticated control plane."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.request

from .archive import file_digest
from .blobs import BlobStore
from .model import WorkloadError
from .download import download_into


class InputTransfer:
    def __init__(self, client, *, max_bytes=2*1024**3):
        self.client = client
        self.max_bytes = max_bytes

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
            raise WorkloadError('upload byte limit exceeded', 413)
        digest = file_digest(source)
        headers = self.headers(assignment)
        headers.update({'Content-Type': 'application/octet-stream', 'Content-Length': str(size)})
        with source.open('rb') as stream:
            request = urllib.request.Request(self.client.url+'objects/'+digest,
                                             data=stream, headers=headers, method='PUT')
            with urllib.request.urlopen(request, timeout=self.client.timeout) as response:
                body = response.read(65537)
                if len(body) > 65536:
                    raise WorkloadError('upload response exceeds bound', 502)
                result = json.loads(body)
                if result.get('digest') != digest or result.get('size') != size:
                    raise WorkloadError('upload acknowledgement mismatch', 502)
                return result

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
                download_into(self.client, digest, self.headers(assignment), out, self.max_bytes)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)
