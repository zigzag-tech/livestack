"""Bounded in-process resume for immutable input downloads."""
import hashlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import http.client
import logging
import re
import time
import urllib.error

from livestack_node import transport

from .model import WorkloadError
from .block_codec import HEADER, DurationExceeded, read_gzip_block
from .liveness import BlockSizer, Liveness, StallError

CHUNK = 4*1024*1024
DEFAULT_LIVENESS = Liveness()


def _stalled(error):
    """True when the transport failure is the socket's no-byte timer (the stall window)."""
    return isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)


def _cause(error):
    return 'stalled' if _stalled(error) else 'duration' if isinstance(error, DurationExceeded) else None


def _last_cause(error):
    """Every budget verdict carries the transport failure that caused it."""
    return '' if error is None else f'; last error: {type(error).__name__}: {error}'


def _fetch_block(client, digest, headers, start, total, deadline, liveness=DEFAULT_LIVENESS):
    """One CHUNK-aligned block of an object whose size is already known.

    Returns the decoded bytes of [start, end], which may be SHORTER than a full block if the
    authority chose a smaller one; the caller stops fanning out on a short block. Refuses
    anything that is not a 206 for exactly this offset of this object.
    """
    requested_end = min(start+CHUNK, total)-1
    deadline = min(deadline, time.monotonic()+liveness.ceiling(requested_end-start+1))
    target, path = transport.split_target(client.url+'objects/'+digest)
    with transport.dial_stream(
            target, 'GET', path, headers={**headers, HEADER: 'gzip', 'Range': f'bytes={start}-{requested_end}'},
            timeout=min(liveness.stall_seconds, max(0.1, deadline-time.monotonic()))) as response:
        length = response.headers.get('Content-Length', '')
        if not re.fullmatch(r'[0-9]{1,20}', length):
            raise WorkloadError('invalid download content length', 502)
        length, codec = int(length), response.headers.get(HEADER)
        if codec not in (None, 'gzip') or response.status != 206:
            raise WorkloadError('authority did not honor a block range', 502)
        match = re.fullmatch(r'bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})', response.headers.get('Content-Range', ''))
        if not match:
            raise WorkloadError('invalid download content range', 502)
        first, end, size = map(int, match.groups())
        if first != start or end < first or end > requested_end or size != total or (codec is None and length != end-first+1):
            raise WorkloadError('download range does not match requested offset', 409)
        etag = response.headers.get('ETag')
        if etag is not None and etag != '"'+digest+'"':
            raise WorkloadError('download object identity changed', 409)
        if codec == 'gzip':
            return read_gzip_block(response, length, end-first+1, deadline)
        data = bytearray()
        while len(data) < length:
            if time.monotonic() >= deadline:
                raise DurationExceeded()
            chunk = response.read1(min(65536, length-len(data)))
            if not chunk:
                raise ConnectionError('download stream ended early')
            data.extend(chunk)
        return bytes(data)


BLOCK_RETRIES = 2


def _transient(error):
    """True for the failures the sequential loop also retries; integrity failures are never transient."""
    if isinstance(error, WorkloadError):
        return error.status not in (409, 413)
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead))


def _parallel_tail(client, digest, headers, out, hasher, count, total, deadline, parallel, liveness=DEFAULT_LIVENESS):
    """Fetch [count, total) with up to `parallel` blocks in flight; return the contiguous count.

    Blocks are written and hashed strictly in order, so `count` is always a contiguous prefix and
    the sequential loop can resume from it. Memory is bounded by parallel x CHUNK. One dropped
    connection must not cost the whole fan-out (on a lossy path it happens every few dozen
    blocks), so a failed block is refetched in place up to BLOCK_RETRIES times; only a block that
    keeps failing, or a short block, returns early so the sequential loop (with its own retry
    budget) finishes the object. An integrity failure raises.
    """
    starts = deque(range(count, total, CHUNK))
    pending, tries = deque(), {}
    submit = lambda pool, start: pool.submit(_fetch_block, client, digest, headers, start, total, deadline, liveness)
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        try:
            while starts or pending:
                while starts and len(pending) < parallel:
                    start = starts.popleft()
                    pending.append((start, submit(pool, start)))
                start, future = pending.popleft()
                try:
                    data = future.result()
                except Exception as error:
                    if not _transient(error):
                        if isinstance(error, urllib.error.HTTPError):
                            error.close()
                        raise
                    if isinstance(error, urllib.error.HTTPError):
                        error.close()
                    tries[start] = tries.get(start, 0)+1
                    if tries[start] > BLOCK_RETRIES or time.monotonic() >= deadline:
                        raise ConnectionError('block at %d failed %d times: %s' % (start, tries[start], error)) from error
                    time.sleep(min(0.25*tries[start], max(0, deadline-time.monotonic())))
                    pending.appendleft((start, submit(pool, start)))  # head of the line: order is preserved
                    continue
                out.write(data); hasher.update(data); count += len(data)
                if len(data) != min(CHUNK, total-start):
                    break
        except ConnectionError as error:
            logging.warning('parallel download %s stopped at %d/%d: %s; finishing sequentially',
                            digest[:12], count, total, error)
        finally:
            for _start, future in pending:
                future.cancel()
    return count


def download_into(client, digest, headers, out, max_bytes, *, max_failures=8, max_seconds=3600, parallel=1, start=0,
                  liveness=None, report=None):
    """`parallel` > 1 fans the blocks after the first out over that many connections. Worth it
    where each request pays a long round trip (the relay measured 0.9-1.9 MB/s sequential against
    ~10 MB/s with four in flight); it never changes what is accepted or how it is verified.

    `start` > 0 resumes an object whose first `start` bytes are already in `out` (opened
    for read+write), typically written through ANOTHER route: the prefix is re-hashed from
    disk so the final digest check still covers every byte, and the first request is a
    range at `start`, never a restart.

    Liveness (liveness.py, openspec object-transfer-liveness): a request is abandoned when no
    byte arrives for the stall window, or when it outlives max(minimum, bytes/floor_rate); either
    way the object resumes at the bytes already saved. The sequential ranges follow measured
    throughput (smaller on a slow path). `max_seconds` is the object's ceiling MINIMUM: a large
    object that keeps moving is allowed max(max_seconds, size/floor_rate). `report`, when given,
    is filled live with the block size and stall events so a failed transfer still explains itself."""
    live = liveness or DEFAULT_LIVENESS
    sizer = BlockSizer(live)
    count, total, failures, requests = start, None, 0, 0
    hasher = hashlib.sha256()
    if start:
        out.seek(0)
        remaining = start
        while remaining:
            block = out.read(min(1024*1024, remaining))
            if not block:
                raise WorkloadError('resume prefix is shorter than its recorded length', 409)
            hasher.update(block)
            remaining -= len(block)
        out.seek(start)
        out.truncate()
    began = time.monotonic()
    deadline = began+max_seconds
    last_error = None
    fanned_out = parallel <= 1
    note = lambda: report.update(sizer.detail()) if report is not None else None
    while total is None or count < total:
        if not fanned_out and total is not None and total-count > CHUNK:
            fanned_out = True  # once: after a fallback the sequential loop finishes the object
            count = _parallel_tail(client, digest, headers, out, hasher, count, total, deadline, parallel, live)
            continue
        if time.monotonic() >= deadline or requests >= (max_bytes+live.min_block-1)//live.min_block+max_failures+1:
            raise WorkloadError('download duration/request budget exhausted'+_last_cause(last_error), 503)
        requests += 1
        block = sizer.block
        requested_end = min(count+block, max_bytes)-1
        ranged = total is not None or count
        attempt_began, attempt_from = time.monotonic(), count
        block_deadline = deadline
        # A normal initial GET also supports empty objects and old authorities.
        # Once interrupted, use bounded ranges starting at bytes actually saved.
        target, path = transport.split_target(client.url+'objects/'+digest)
        try:
            with transport.dial_stream(
                    target, 'GET', path,
                    headers={**headers, HEADER: 'gzip', **({'Range': f'bytes={count}-{max(count, requested_end)}'} if ranged else {})},
                    timeout=min(live.stall_seconds, max(0.1, deadline-time.monotonic()))) as response:
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
                if total is None:
                    # The object's own ceiling, now that its size is known: a transfer that keeps
                    # moving is not bounded by a fixed clock.
                    deadline = max(deadline, began+live.ceiling(size, minimum=max_seconds))
                total, remaining = size, length
                block_deadline = min(deadline, attempt_began+live.ceiling(length))
                if codec == 'gzip':
                    decoded = read_gzip_block(response, length, end-start+1, block_deadline)
                    out.write(decoded); hasher.update(decoded); count += len(decoded)
                else:
                    while remaining:
                        if time.monotonic() >= block_deadline:
                            raise DurationExceeded('download duration budget exhausted'+_last_cause(last_error))
                        chunk = response.read1(min(65536, remaining))
                        if not chunk:
                            raise ConnectionError('download stream ended early')
                        out.write(chunk)
                        hasher.update(chunk)
                        count += len(chunk)
                        remaining -= len(chunk)
                if ranged:
                    sizer.ok(count-attempt_from, time.monotonic()-attempt_began, requested_end-attempt_from+1)
                note()
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise
            error.close()
            failures += 1
            last_error = error
        except DurationExceeded as error:
            if time.monotonic() >= deadline:
                raise
            failures += 1
            last_error = error
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
            failures += 1
            last_error = error
        else:
            continue
        cause = _cause(last_error)
        if cause == 'stalled':
            last_error = StallError('stalled: no byte for %gs' % live.stall_seconds)
        sizer.failed(cause)
        note()
        logging.warning('download %s failed %d/%d after %d bytes (%s): %s',
                        digest[:12], failures, max_failures, count, cause or 'transport', last_error)
        if failures > max_failures:
            raise WorkloadError('download retry budget exhausted'+_last_cause(last_error), 503) from last_error
        time.sleep(min(0.25*2**(failures-1), 2, max(0, deadline-time.monotonic())))
    if count != total or hasher.hexdigest() != digest:
        raise WorkloadError('download content does not match its identity', 409)
