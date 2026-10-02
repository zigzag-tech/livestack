"""Small stdlib client usable by workers and product-specific adapters."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from livestack_node import transport

from .model import WorkloadError, encode


class WorkloadClient:
    def __init__(self, url, token, *, timeout=15):
        if not url.startswith(('http://', 'https://')) or len(token) < 32:
            raise ValueError('a workload URL and strong credential are required')
        self.url = url.rstrip('/') + '/v1/workloads/'
        self.token = token
        self.timeout = timeout
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
        return WorkloadClient(self.url[:-len('/v1/workloads/')], self.token, timeout=self.timeout)

    def close(self):
        if self._kept is not None:
            self._kept.close()

    def request(self, route, body=None):
        target, path = transport.split_target(self.url + route)
        dial = (transport.dial if self._kept is None else
                lambda _target, method, path, **kw: self._kept.request(
                    method, path, headers=kw['headers'], body=kw['body']))
        try:
            _status, _headers, data = dial(
                target, 'POST' if body is not None else 'GET', path,
                headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'},
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
        return self.request('jobs', request)

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

    def list_jobs(self):
        """This principal's most recent jobs (the authority bounds the page)."""
        return self.request('jobs')['jobs']
