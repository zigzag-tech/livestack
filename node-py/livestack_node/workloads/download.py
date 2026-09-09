"""Bounded in-process resume for immutable input downloads."""
import hashlib
import http.client
import re
import time
import urllib.error
import urllib.request

from .model import WorkloadError
from .block_codec import HEADER, read_gzip_block

CHUNK = 4*1024*1024


def download_into(client, digest, headers, out, max_bytes, *, max_failures=8, max_seconds=3600):
    count, total, failures, requests = 0, None, 0, 0
    hasher = hashlib.sha256()
    deadline = time.monotonic()+max_seconds
    while total is None or count < total:
        if time.monotonic() >= deadline or requests >= (max_bytes+CHUNK-1)//CHUNK+max_failures+1:
            raise WorkloadError('download duration/request budget exhausted', 503)
        requests += 1
        requested_end = min(count+CHUNK, max_bytes)-1
        # A normal initial GET also supports empty objects and old authorities.
        # Once interrupted, use bounded ranges starting at bytes actually saved.
        request = urllib.request.Request(client.url+'objects/'+digest,
            headers={**headers, HEADER: 'gzip', **({'Range': f'bytes={count}-{max(count, requested_end)}'} if total is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=min(client.timeout, max(0.1, deadline-time.monotonic()))) as response:
                length = response.headers.get('Content-Length', '')
                if not re.fullmatch(r'[0-9]{1,20}', length):
                    raise WorkloadError('invalid download content length', 502)
                length = int(length)
                codec = response.headers.get(HEADER)
                if codec not in (None, 'gzip'):
                    raise WorkloadError('unsupported block encoding', 502)
                if codec and response.status != 206:
                    raise WorkloadError('compressed block requires a range', 502)
                if response.status == 206:
                    match = re.fullmatch(r'bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})', response.headers.get('Content-Range', ''))
                    if not match:
                        raise WorkloadError('invalid download content range', 502)
                    start, end, size = map(int, match.groups())
                    if start != count or end < start or end > requested_end or end >= size or (codec is None and length != end-start+1):
                        raise WorkloadError('download range does not match requested offset', 409)
                elif response.status == 200 and count == 0:
                    # Older authorities ignore Range. A complete first reply
                    # remains usable, but an ignored resume never duplicates bytes.
                    size = length
                else:
                    raise WorkloadError('authority did not honor download resume', 409)
                if size > max_bytes or (total is not None and total != size):
                    raise WorkloadError('download size changed or exceeds byte bound', 413)
                etag = response.headers.get('ETag')
                if etag is not None and etag != '"'+digest+'"':
                    raise WorkloadError('download object identity changed', 409)
                total, remaining = size, length
                if codec == 'gzip':
                    decoded = read_gzip_block(response, length, end-start+1, deadline)
                    out.write(decoded); hasher.update(decoded); count += len(decoded)
                    continue
                while remaining:
                    if time.monotonic() >= deadline:
                        raise WorkloadError('download duration budget exhausted', 503)
                    chunk = response.read1(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError('download stream ended early')
                    out.write(chunk)
                    hasher.update(chunk)
                    count += len(chunk)
                    remaining -= len(chunk)
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
            error.close()
            failures += 1
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            failures += 1
        else:
            continue
        if failures > max_failures:
            raise WorkloadError('download retry budget exhausted', 503)
        time.sleep(min(0.25*2**(failures-1), 2, max(0, deadline-time.monotonic())))
    if count != total or hasher.hexdigest() != digest:
        raise WorkloadError('download content does not match its identity', 409)
