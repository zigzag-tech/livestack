"""Consumer client — lease-then-call over the /livestack REST facade, with
graceful degradation.

    with lease("diarize", base_url="http://127.0.0.1:8766/livestack"):
        requests.post(f"{POLYASR}/v1/diarize", ...)   # warm + protected

If ``base_url`` is omitted or the endpoint is unreachable (a standalone server
with no /livestack), yields a no-op lease and the call still hits the raw server,
which self-manages residence. Uses only the stdlib so consumers need no deps.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Iterator, Optional


class _NoopLease:
    lease_id = None


@contextmanager
def lease(
    kind: str,
    *,
    base_url: Optional[str] = None,
    owner_id: str = "consumer",
    ttl_seconds: int = 120,
    heartbeat_interval: float = 30.0,
) -> Iterator[object]:
    if base_url is None:
        yield _NoopLease()
        return
    base = base_url.rstrip("/")
    try:
        res = _post(f"{base}/lease", {"kind": kind, "owner_id": owner_id, "ttl_seconds": ttl_seconds})
    except (urllib.error.URLError, OSError):
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
