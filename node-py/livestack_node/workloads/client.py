"""Small stdlib client usable by workers and product-specific adapters."""
from __future__ import annotations

import json
import urllib.error

from livestack_node import transport

from .model import WorkloadError, encode


class WorkloadClient:
    def __init__(self, url, token, *, timeout=15):
        if not url.startswith(('http://', 'https://')) or len(token) < 32:
            raise ValueError('a workload URL and strong credential are required')
        self.url = url.rstrip('/') + '/v1/workloads/'
        self.token = token
        self.timeout = timeout

    def request(self, route, body=None):
        target, path = transport.split_target(self.url + route)
        try:
            _status, _headers, data = transport.dial(
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
