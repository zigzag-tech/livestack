"""Built-in route kinds: `http` (an authority endpoint) and `edge_relay` (the same
authority behind a metered, budgeted forwarder).

Both speak the authority's object API, so a transfer that fails on one route
resumes on another at the offset the authority already holds (uploads) or the
offset already on disk (downloads). New kinds (mesh tunnel, object-store bus,
LAN peer) register through routes.register_kind; see node-py/docs/transport-routes.md.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import time
import urllib.error

from livestack_node import transport

from .download import download_into
from .model import WorkloadError
from .routes import Route, register_kind

UPLOAD_CHUNK = 8*1024*1024
CHUNK_RETRIES = 2
STATUS_TTL = 30
TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException)


class HttpRoute(Route):
    """`client` is a WorkloadClient aimed at this route's endpoint (credentials stay
    with the caller, never in the descriptor). `edge_key` marks a relay."""

    def __init__(self, descriptor, client, *, edge_key=None, parallel=1, clock=time.monotonic):
        super().__init__(descriptor)
        self.client, self.edge_key, self.parallel, self.clock = client, edge_key, parallel, clock
        self.chunk = descriptor.options.get('chunk_bytes', UPLOAD_CHUNK)
        self._status = (0.0, None)
        self._resumable = None   # learned per route: does the endpoint have objects/<d>/upload?

    # -- pre-flight -----------------------------------------------------
    def unavailable(self):
        if self.descriptor.kind != 'edge_relay':
            return None
        checked, reason = self._status
        if checked and self.clock()-checked < STATUS_TTL:
            return reason
        try:
            target, path = transport.split_target(self.client.url.replace('/v1/workloads/', '/v1/edge/status'))
            status, _headers, body = transport.dial(target, 'GET', path, headers={}, timeout=5)
            state = json.loads(body).get('state')
            reason = None if status == 200 and state == 'ok' else (state or 'status %s' % status)
        except (OSError, ValueError, WorkloadError) as error:
            reason = 'unreachable: %s' % error
        self._status = (self.clock(), reason)
        return reason

    def headers(self, base):
        headers = dict(base)
        if self.edge_key is not None:
            headers['X-Edge-Key'] = self.edge_key
        return headers

    # -- upload ---------------------------------------------------------
    def _dial(self, method, path, headers, body=None):
        target, path = transport.split_target(self.client.url+path)
        return transport.dial(target, method, path, headers=headers, body=body, timeout=self.client.timeout)

    def _offset(self, digest, headers):
        """(staged bytes, size when already complete), or None when no resumable route."""
        try:
            _s, _h, body = self._dial('GET', 'objects/%s/upload' % digest, headers)
            state = json.loads(body)
            offset = state.get('offset')
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                return None
            return offset, (state.get('size') if state.get('complete') else None)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (404, 405):
                return None
            raise

    def upload(self, source, digest, size, ctx):
        headers = self.headers(ctx['headers'])
        state = None
        if self._resumable is not False:
            state = self._offset(digest, headers)
            self._resumable = state is not None
        if not self._resumable:
            return self._upload_whole(source, digest, size, headers, ctx)
        offset, complete = state
        if complete == size:
            return {'digest': digest, 'size': size}  # a lost final ack: the authority already holds it
        moved = 0
        failures = 0
        with source.open('rb') as stream:
            while True:
                length = min(self.chunk, size-offset) if size else 0
                if length <= 0:
                    break  # empty objects go through the whole-object path below
                stream.seek(offset)
                data = stream.read(length)
                if len(data) != length:
                    raise WorkloadError('source changed during upload', 409)
                chunk_headers = dict(headers, **{
                    'Content-Type': 'application/octet-stream', 'Content-Length': str(length),
                    'Content-Range': 'bytes %d-%d/%d' % (offset, offset+length-1, size),
                    'X-Chunk-Digest': hashlib.sha256(data).hexdigest()})
                try:
                    _s, _h, body = self._dial('PUT', 'objects/%s/upload' % digest, chunk_headers, data)
                    ack = json.loads(body)
                    if ack.get('offset') != offset+length:
                        raise WorkloadError('upload chunk acknowledgement mismatch', 502)
                except urllib.error.HTTPError as error:
                    detail = {}
                    try:
                        detail = json.loads(error.read(65536))
                    except (ValueError, OSError):
                        pass
                    error.close()
                    if error.code == 409 and isinstance(detail.get('offset'), int) and detail['offset'] != offset:
                        # An earlier try landed (lost ack) or another route moved the
                        # offset: resynchronise on the authority's count, never resend.
                        logging.info('upload %s resync %d -> %d', digest[:12], offset, detail['offset'])
                        offset = detail['offset']
                        if offset >= size:
                            break
                        continue
                    if error.code not in (400, 408, 429, 500, 502, 503, 504) or failures >= CHUNK_RETRIES:
                        raise
                    failures += 1
                    continue
                except TRANSIENT + (WorkloadError, ValueError) as error:
                    failures += 1
                    if failures > CHUNK_RETRIES:
                        raise
                    logging.warning('upload %s chunk at %d failed (%s); retrying', digest[:12], offset, error)
                    continue
                failures = 0
                offset += length
                moved += length
                ctx['moved'] = ctx.get('moved', 0)+length
                if ack.get('complete'):
                    return {'digest': ack['digest'], 'size': ack['size']}
        if size == 0 or offset >= size:
            # Whole object staged but the final ack was lost: the whole-object PUT
            # of a ready object is the verifying no-op, so it is the safe finisher.
            return self._upload_whole(source, digest, size, headers, ctx)
        raise WorkloadError('upload ended before completion', 502)

    def _upload_whole(self, source, digest, size, headers, ctx):
        headers = dict(headers, **{'Content-Type': 'application/octet-stream', 'Content-Length': str(size)})
        with source.open('rb') as stream:
            _s, _h, body = self._dial('PUT', 'objects/'+digest, headers, stream)
        if len(body) > 65536:
            raise WorkloadError('upload response exceeds bound', 502)
        result = json.loads(body)
        if result.get('digest') != digest or result.get('size') != size:
            raise WorkloadError('upload acknowledgement mismatch', 502)
        ctx['moved'] = ctx.get('moved', 0)+size
        return result

    # -- download -------------------------------------------------------
    def download(self, digest, out, ctx):
        start = out.tell()
        failures = self.descriptor.options.get('max_failures', 2 if self.descriptor.kind == 'edge_relay' else None)
        options = {} if failures is None else {'max_failures': failures}
        if self.parallel > 1:
            options['parallel'] = self.parallel
        try:
            download_into(self.client, digest, self.headers(ctx['headers']), out, ctx['max_bytes'], start=start,
                          **options)
        finally:
            ctx['moved'] = ctx.get('moved', 0)+max(0, out.tell()-start)


register_kind('http', lambda descriptor, **context: HttpRoute(descriptor, **context))
register_kind('edge_relay', lambda descriptor, **context: HttpRoute(descriptor, **context))
