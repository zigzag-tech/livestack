"""Authenticated immutable blob streaming, including a single byte range."""
import logging
import re

from .model import WorkloadError


def send_object(handler, stream, size, digest):
    requested = handler.headers.get('Range')
    start, end = 0, size-1
    if requested is not None:
        match = re.fullmatch(r'bytes=([0-9]{1,20})-([0-9]{0,20})', requested)
        if not match:
            raise WorkloadError('only a single explicit byte range is supported', 416)
        start = int(match[1])
        end = min(int(match[2]), size-1) if match[2] else size-1
        if start > end or start >= size:
            raise WorkloadError('object range is outside content', 416)
    stream.seek(start)
    handler.send_response(206 if requested is not None else 200)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Length', str(max(0, end-start+1)))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('ETag', '"'+digest+'"')
    if requested is not None:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    handler.send_header('Connection', 'close')
    # From here any failure closes the connection. Sending JSON status here
    # would append HTTP headers/error bytes inside a partial binary response.
    handler.close_connection = True
    try:
        handler.end_headers()
        remaining = max(0, end-start+1)
        while remaining:
            # sendall's deadline covers the entire write, even while bytes are
            # advancing. A 1 MiB write timed out on the China path despite
            # continual reads; keep each operation small enough to make progress.
            chunk = stream.read(min(16*1024, remaining))
            if not chunk:
                raise OSError('object ended before declared content length')
            handler.wfile.write(chunk)
            remaining -= len(chunk)
    except OSError:
        logging.warning('workload object stream interrupted; range retry required')
