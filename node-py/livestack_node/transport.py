"""The fleet dial seam: every outbound HTTP dial between livestack fleet
processes (node -> broker, broker -> node facade, worker -> authority,
perception forwarding, policy-lab profiling) goes through this module.

`KeptConnection` is the persistent form: one HTTP/1.1 connection reused
across requests, for control traffic that must not need a fresh TCP handshake
per request (lease renewal; openspec/changes/worker-control-keepalive).

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

Mesh dials do NOT flow through this seam: a `mesh://` target is refused here
by name (see `_refuse_mesh_scheme`). Mesh needs a per-request WSS stream, a
minted `bdsr1` capability and route policy from the mesh-route Picker, all of
which live in MeshPeer; peers are scheme-selected at construction
(hostd.make_peer), so the (target, method, path, headers, body) contract here
stays urllib-shaped HTTP only.
"""
from __future__ import annotations

import http.client
import io
import socket
import threading
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


def _refuse_mesh_scheme(target: str) -> None:
    """Mesh dials do NOT flow through this seam: a `mesh://` target needs a
    per-request WSS stream, a minted `bdsr1` capability and route policy from
    the mesh-route Picker — all of which live in MeshPeer (hostd.make_peer).
    urllib would only fail here as an 'unknown url type' error that names
    nothing; refusing at the seam names the right door."""
    if target.startswith("mesh://"):
        raise ValueError(
            f"mesh:// target {target!r} dials through MeshPeer, not "
            "transport.dial — peers are scheme-selected at construction")


def dial_stream(target: str, method: str, path: str,
                headers: Optional[Mapping[str, str]] = None,
                body: Body = None,
                timeout: float = 30.0):
    """One HTTP request; returns the live response object (context manager with
    `.status`, `.headers`, `.read(n)`, `.read1(n)`, `.close()`).

    Error behavior is urllib.urlopen's, unchanged: `HTTPError` on HTTP >= 400
    (error body readable from the exception), `URLError`/`TimeoutError` on
    connection failure."""
    _refuse_mesh_scheme(target)
    url = target.rstrip("/") + (path if path.startswith("/") else "/" + path)
    req = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method)
    return urllib.request.urlopen(req, timeout=timeout)


class Cancelled(URLError):
    """A dial cut by its `Cancel` (a URLError, so existing handlers still catch it)."""


class Cancel:
    """A caller's way to abandon its dials: `cancel()` cuts every dial in
    flight that was made with this object, and later ones fail at once.

    For a process that relays its own caller's request through a long call
    (a lease that queues for minutes, a generation that runs for twenty) and
    must stop when that caller hangs up. Cutting the socket is what tells the
    far end: a dial left running in an abandoned thread keeps its connection
    open, and the far end keeps working (or queueing) for nobody."""

    def __init__(self):
        self._lock = threading.Lock()
        self._socks = set()
        self.cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            socks = list(self._socks)
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _hold(self, sock) -> None:
        with self._lock:
            if self.cancelled:
                raise Cancelled("cancelled")
            self._socks.add(sock)

    def _release(self, sock) -> None:
        with self._lock:
            self._socks.discard(sock)


def _dial_cancellable(target, method, path, headers, body, timeout, cancel: Cancel):
    """`dial` on a socket `cancel` can cut. Plain http.client, like
    `KeptConnection`: no redirects or proxies, so for direct control dials."""
    _refuse_mesh_scheme(target)
    parts = urllib.parse.urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"cancellable dial needs an http(s) target: {target!r}")
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    if not path.startswith("/"):
        path = "/" + path
    url = target.rstrip("/") + path
    conn = cls(parts.netloc, timeout=timeout)
    try:
        try:
            conn.connect()
            sock = conn.sock
            cancel._hold(sock)
            try:
                conn.request(method, parts.path.rstrip("/") + path,
                             body=body, headers=dict(headers or {}))
                response = conn.getresponse()
                data = response.read()
            finally:
                cancel._release(sock)
        except Cancelled:
            raise
        except (OSError, http.client.HTTPException) as error:
            if cancel.cancelled:
                raise Cancelled("cancelled") from error
            raise URLError(error) from error
    finally:
        conn.close()
    if response.status >= 400:
        raise HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(data))
    return response.status, response.headers, data


def dial(target: str, method: str, path: str,
         headers: Optional[Mapping[str, str]] = None,
         body: Body = None,
         timeout: float = 30.0,
         cancel: Optional[Cancel] = None) -> Tuple[int, Headers, bytes]:
    """One HTTP request, response buffered. Returns `(status, headers, body)`.

    Same error surface as `dial_stream`: HTTP >= 400 raises `HTTPError`,
    connection failure raises `URLError`/`TimeoutError` — the (status, ...)
    tuple is the SUCCESS-path shape. With `cancel`, the dial can be cut
    (raising `Cancelled`) and goes direct, without urllib's proxy handling."""
    if cancel is not None:
        return _dial_cancellable(target, method, path, headers, body, timeout, cancel)
    with dial_stream(target, method, path, headers=headers, body=body,
                     timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


# Failures that mean "the server closed a kept connection before answering":
# raised by the request write or the status-line read of a REUSED connection.
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
          ConnectionAbortedError)


def _keepalive(sock) -> None:
    """Probe an idle kept connection so a peer that vanished without a FIN/RST
    (authority host down, NAT/tunnel state dropped) fails the next call within
    about a minute instead of sitting half-open. Best effort: platforms without
    the knobs keep the per-call socket timeout, which already bounds every read."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


class KeptConnection:
    """One persistent HTTP/1.1 connection to `target`, response buffered.

    `request()` has `dial`'s contract: `(status, headers, body)` on success,
    `HTTPError` on HTTP >= 400 (body readable), `URLError` on connection
    failure. Requests are serialised by a lock. A request that fails on a
    REUSED connection before any response byte arrives (the server closed it
    while idle) is retried once on a fresh connection; every other failure
    closes the connection and is raised for the caller's own retry policy.
    A response with `Connection: close` is honoured: the next request
    reconnects."""

    def __init__(self, target: str, timeout: float = 30.0):
        _refuse_mesh_scheme(target)
        parts = urllib.parse.urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"kept connection needs an http(s) target: {target!r}")
        self.target = target.rstrip("/")
        self._cls = (http.client.HTTPSConnection if parts.scheme == "https"
                     else http.client.HTTPConnection)
        self._netloc = parts.netloc
        self.timeout = timeout
        self._conn = None
        self._served = 0  # responses read on the current connection
        self._lock = threading.Lock()
        self.connects = 0  # TCP connections opened, for observability/tests

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn, self._served = None, 0

    def _once(self, method, path, headers, body):
        if self._conn is None:
            self._conn = self._cls(self._netloc, timeout=self.timeout)
            self._conn.connect()
            _keepalive(self._conn.sock)
            self.connects += 1
        self._conn.request(method, path, body=body, headers=headers)
        response = self._conn.getresponse()
        data = response.read()
        self._served += 1
        if response.will_close:
            self._drop()
        return response, data

    def request(self, method: str, path: str,
                headers: Optional[Mapping[str, str]] = None,
                body: Optional[bytes] = None) -> Tuple[int, Headers, bytes]:
        if not path.startswith("/"):
            path = "/" + path
        headers = dict(headers or {})
        with self._lock:
            try:
                try:
                    response, data = self._once(method, path, headers, body)
                except _STALE:
                    if not self._served:
                        raise
                    self._drop()
                    response, data = self._once(method, path, headers, body)
            except (OSError, http.client.HTTPException) as error:
                self._drop()
                raise URLError(error) from error
        if response.status >= 400:
            raise HTTPError(self.target + path, response.status, response.reason,
                            response.headers, io.BytesIO(data))
        return response.status, response.headers, data
