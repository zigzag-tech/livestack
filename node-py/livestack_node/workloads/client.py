"""Small stdlib client usable by workers and product-specific adapters."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from livestack_node import transport

from .origin import stamp
from .model import WorkloadError, encode


class WorkloadClient:
    def __init__(self, url, token, *, timeout=15, edge_key=None):
        if not url.startswith(('http://', 'https://')) or len(token) < 32:
            raise ValueError('a workload URL and strong credential are required')
        if edge_key is not None and (not isinstance(edge_key, str) or len(edge_key) < 32):
            raise ValueError('an edge key must contain at least 32 characters')
        self.url = url.rstrip('/') + '/v1/workloads/'
        self.token = token
        self.timeout = timeout
        self._capability_cache = None
        self._capability_checked = 0.0
        self.edge_key = edge_key
        # Control requests ride one kept-alive connection, so an established
        # worker needs no new TCP handshake per request
        # (openspec/changes/worker-control-keepalive). urllib would have sent
        # the request through an environment proxy; keep that path unchanged.
        target = transport.split_target(self.url)[0]
        parts = urllib.parse.urlsplit(target)
        proxied = (parts.scheme in urllib.request.getproxies()
                   and not urllib.request.proxy_bypass(parts.hostname or ''))
        self._kept = None if proxied else transport.KeptConnection(target, timeout)

    def channel(self):
        """A client with its own kept connection to the same authority: the
        lease keeper renews on it, never queued behind this client's requests."""
        return WorkloadClient(self.url[:-len('/v1/workloads/')], self.token, timeout=self.timeout,
                              edge_key=self.edge_key)

    def close(self):
        if self._kept is not None:
            self._kept.close()

    def request(self, route, body=None):
        target, path = transport.split_target(self.url + route)
        dial = (transport.dial if self._kept is None else
                lambda _target, method, path, **kw: self._kept.request(
                    method, path, headers=kw['headers'], body=kw['body']))
        try:
            headers = {'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'}
            if self.edge_key is not None:
                headers['X-Edge-Key'] = self.edge_key
            _status, _headers, data = dial(
                target, 'POST' if body is not None else 'GET', path,
                headers=headers,
                body=encode(body).encode() if body is not None else None,
                timeout=self.timeout)
            if len(data) > 8*1024*1024:
                raise WorkloadError('authority response exceeds byte limit', 502)
            return json.loads(data)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(65536)).get('error', 'workload request refused')
            except (ValueError, AttributeError):
                detail = 'workload request refused'
            raise WorkloadError(detail, error.code) from error

    def submit(self, request):
        request = stamp(request)
        if isinstance(request, dict) and request.get('version') == 3 and request.get('environment') is not None:
            capabilities = self.capabilities()
            environment = capabilities.get('environments') or {}
            handler = request.get('handler')
            if 3 not in capabilities.get('versions', []):
                raise WorkloadError('environment_unsupported: schema 3 is unavailable', 409)
            if environment.get('version') != 1:
                raise WorkloadError('environment_unsupported: authority environment API unavailable', 409)
            if handler in environment.get('forbidden_handlers', []):
                raise WorkloadError('environment_scope_forbidden', 403)
            if handler not in environment.get('handlers', []):
                raise WorkloadError('environment_unsupported: handler is not enrolled', 409)
        try:
            return self.request('jobs', request)
        except WorkloadError as error:
            if error.status in (400, 409) and ('unsupported workload schema' in str(error) or
                                                'environment_unsupported' in str(error)):
                self._capability_cache = None
                self._capability_checked = 0.0
            raise

    def capabilities(self, *, refresh=False):
        """Read bounded authenticated authority capabilities; cache for at most 60s."""
        now = time.monotonic()
        if not refresh and self._capability_cache is not None and now-self._capability_checked < 60:
            return self._capability_cache
        try:
            result = self.request('capabilities')
        except WorkloadError as error:
            if error.status in (404, 405, 501):
                raise WorkloadError('environment_unsupported: authority capability API unavailable', 409) from error
            raise
        if (not isinstance(result, dict) or not isinstance(result.get('versions'), list) or
                not isinstance(result.get('environments'), dict) or len(encode(result).encode()) > 8192):
            raise WorkloadError('invalid capability response', 502)
        self._capability_cache, self._capability_checked = result, now
        return result

    def get_environment(self, handle):
        from .model import name
        result = self.request('environments/'+name(handle, 'environment handle'))
        if len(encode(result).encode()) > 16*1024:
            raise WorkloadError('environment response exceeds byte limit', 502)
        return result

    def get(self, job_id):
        from .model import name
        return self.request('jobs/'+name(job_id, 'job_id'))

    def cancel(self, job_id):
        """Owner cancel: a queued job ends now, a running one is fenced and its
        worker cleans up. Idempotent on a terminal job."""
        from .model import name
        return self.request('jobs/'+name(job_id, 'job_id')+'/cancel', {})

    def withdraw(self, job_id):
        """Cancel only if no worker ever attempted the job; otherwise the job
        is returned unchanged. Read `state` for the outcome."""
        from .model import name
        return self.request('jobs/'+name(job_id, 'job_id')+'/withdraw', {})

    def roster(self):
        """Read-only fleet roster as the authority sees it (workloads/roster.py)."""
        return self.request('workers')

    def list_jobs(self):
        """This principal's most recent jobs (the authority bounds the page)."""
        return self.request('jobs')['jobs']
