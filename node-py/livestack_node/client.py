"""Consumer client — lease-then-call with graceful degradation.

Usage in a pipeline (e.g. media-corpus)::

    with lease("diarize", base_url="http://127.0.0.1:8766/livestack"):
        requests.post(f"{POLYASR}/v1/diarize", ...)   # warm + protected

Three modes, all via the same context manager:
- embedded: pass ``controller=`` to lease against an in-process controller.
- remote:   pass ``base_url=`` to lease over the REST facade (heartbeated by a
            background daemon thread).
- no-op:    neither given, or the remote endpoint is unreachable -> yields a
            no-op lease and the call still hits the raw server (which
            self-manages via its own sweep). Standalone correctness preserved.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Iterator, Optional

from .residence import ResidenceController


class _NoopLease:
    lease_id = None
    active = False


@contextmanager
def lease(
    kind: str,
    *,
    controller: Optional[ResidenceController] = None,
    base_url: Optional[str] = None,
    owner_id: str = "consumer",
    ttl_seconds: int = 120,
    heartbeat_interval: float = 30.0,
) -> Iterator[object]:
    if controller is not None:
        yield from _embedded_lease(controller, kind, owner_id, ttl_seconds, heartbeat_interval)
    elif base_url is not None:
        yield from _remote_lease(base_url, kind, owner_id, ttl_seconds, heartbeat_interval)
    else:
        yield _NoopLease()


def _embedded_lease(controller, kind, owner_id, ttl_seconds, heartbeat_interval):
    acquired = controller.acquire(kind=kind, owner_id=owner_id, ttl_seconds=ttl_seconds)
    if acquired is None:
        # No capacity -> degrade rather than block the caller.
        yield _NoopLease()
        return
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(heartbeat_interval):
            controller.heartbeat(acquired.lease_id, ttl_seconds=ttl_seconds)

    beater = threading.Thread(target=_beat, name="lease-heartbeat", daemon=True)
    beater.start()
    try:
        yield acquired
    finally:
        stop.set()
        beater.join(timeout=1.0)
        controller.release(acquired.lease_id)


def _remote_lease(base_url, kind, owner_id, ttl_seconds, heartbeat_interval):
    base = base_url.rstrip("/")
    try:
        res = _post(f"{base}/lease", {"kind": kind, "owner_id": owner_id, "ttl_seconds": ttl_seconds})
    except (urllib.error.URLError, OSError):
        # Server has no lease support / is unreachable -> standalone path.
        yield _NoopLease()
        return
    lease_id = res.get("lease_id")
    if not lease_id:
        yield _NoopLease()
        return
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(heartbeat_interval):
            try:
                _post(f"{base}/lease/{lease_id}/heartbeat", {"ttl_seconds": ttl_seconds})
            except (urllib.error.URLError, OSError):
                return

    beater = threading.Thread(target=_beat, name="lease-heartbeat", daemon=True)
    beater.start()
    try:
        yield res
    finally:
        stop.set()
        beater.join(timeout=1.0)
        try:
            _post(f"{base}/lease/{lease_id}/release", {})
        except (urllib.error.URLError, OSError):
            pass


def _post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}
