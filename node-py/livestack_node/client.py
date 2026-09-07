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


def admit(kind: str, *, owner_id: str = "node",
          timeout: float = 240.0, brokers: Optional[list] = None) -> dict:
    """Ask Harmony to MAKE ROOM for `kind`, and say where it granted it.

    This is the missing half of arbitration. A node that loads a unit straight
    off an incoming request never asks the planner anything, so the planner
    never evicts anybody, and the load runs into whatever memory happens to be
    free. Observed exactly that way on xc-tower-ubuntu, 2026-09-06:

        ValueError: Free memory on device cuda:0 (6.9/23.56 GiB) on startup is
        less than desired GPU memory utilization (0.62, 14.61 GiB)

    5 GB of ASR and 5 GB of TTS were sitting idle on that card and would have
    been evicted the moment anyone asked. Nobody asked. Harmony can only
    arbitrate what goes through it, and a direct call to a node went around it.

    `/admit` plans AND dispatches: victims are evicted on their own nodes before
    this returns, so the caller may load as soon as it sees its own device in
    `device_id`.

    Degradation is deliberate and one-way: if no broker answers, the result is
    `granted: True` with `device_id: None` and a `degraded` reason, and the
    caller loads as it always did. An arbitration outage must not take a model
    offline — it only costs the arbitration.
    """
    from .announce import broker_urls
    # No device selector: WHERE it goes is the planner's decision, and pinning it
    # here would be the caller deciding placement again.
    body = {"kind": kind, "owner": owner_id}
    last = None
    for base in (brokers if brokers is not None else broker_urls()):
        try:
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(f"{base.rstrip('/')}/admit", data=data,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            continue
    return {"granted": True, "device_id": None,
            "degraded": f"no broker answered ({last})"}
