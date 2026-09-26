"""The fleet dial seam: every outbound HTTP dial between livestack fleet
processes (node -> broker, broker -> node facade, worker -> authority,
perception forwarding, policy-lab profiling) goes through this module.

`dial` is the buffered form: one call, one (status, headers, body) tuple.
`dial_stream` is the incremental form, for the two callers whose semantics
require reading the response as it arrives — the resumable download loop
(retries a truncated stream from where it stopped) and the policy-lab
profiler (measures time-to-first-byte, which buffering would flatten into
time-to-completion).

The default implementation IS urllib, moved here rather than rewritten:
redirects, TLS verification, header casing, and the error surface
(`urllib.error.HTTPError` on HTTP >= 400 with the error body readable,
`urllib.error.URLError` / `TimeoutError` / `OSError` on connection failure)
are urllib's own, so call sites keep their existing `except` clauses.

Later phases put a second implementation behind this seam (meshlink tunnels,
Phase 5 MeshPeer). The signature is the contract; a `mesh://` target will
resolve through the same `(target, method, path, headers, body)` call.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import BinaryIO, Mapping, Optional, Tuple, Union

# urllib accepts bytes or a readable binary stream as the request body (the
# stream form rides an explicit Content-Length header, see workloads/transfer).
Body = Union[bytes, BinaryIO, None]

# urllib's response headers (http.client.HTTPMessage): a case-insensitive
# mapping with .get(). Implementations behind this seam must return an object
# with the same .get() surface.
Headers = Mapping[str, str]

# Re-exported so call sites can catch the seam's error types without importing
# urllib themselves; the default implementation raises exactly these.
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError


def split_target(url: str) -> Tuple[str, str]:
    """`'http://host:port/a/b?c=1'` -> `('http://host:port', '/a/b?c=1')`.

    Call sites that already hold one assembled URL string use this to fit the
    (target, path) seam signature."""
    parts = urllib.parse.urlsplit(url)
    target = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return target, path


def dial_stream(target: str, method: str, path: str,
                headers: Optional[Mapping[str, str]] = None,
                body: Body = None,
                timeout: float = 30.0):
    """One HTTP request; returns the live response object (context manager with
    `.status`, `.headers`, `.read(n)`, `.read1(n)`, `.close()`).

    Error behavior is urllib.urlopen's, unchanged: `HTTPError` on HTTP >= 400
    (error body readable from the exception), `URLError`/`TimeoutError` on
    connection failure."""
    url = target.rstrip("/") + (path if path.startswith("/") else "/" + path)
    req = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def dial(target: str, method: str, path: str,
         headers: Optional[Mapping[str, str]] = None,
         body: Body = None,
         timeout: float = 30.0) -> Tuple[int, Headers, bytes]:
    """One HTTP request, response buffered. Returns `(status, headers, body)`.

    Same error surface as `dial_stream`: HTTP >= 400 raises `HTTPError`,
    connection failure raises `URLError`/`TimeoutError` — the (status, ...)
    tuple is the SUCCESS-path shape."""
    with dial_stream(target, method, path, headers=headers, body=body,
                     timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()
