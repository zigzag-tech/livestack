"""Small stdlib client usable by workers and product-specific adapters."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .model import WorkloadError, encode


class WorkloadClient:
    def __init__(self, url, token, *, timeout=15):
        if not url.startswith(('http://', 'https://')) or len(token) < 32:
            raise ValueError('a workload URL and strong credential are required')
        self.url = url.rstrip('/') + '/v1/workloads/'
        self.token = token
        self.timeout = timeout

    def request(self, route, body=None):
        req = urllib.request.Request(self.url + route,
            data=encode(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = response.read(8*1024*1024 + 1)
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
