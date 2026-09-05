"""announce.py — the node half of peer membership: reporting for duty.

Node-side I/O only; the decisions live in `membership.py`. A node calls this
once from `attach()` and then forgets about it.

Why it retries forever rather than registering once at startup: start order is
not something a fleet should have to arrange. The broker may come up after its
nodes, or restart later with an empty roster — the broker keeps no durable
state, on purpose. A node that renews on an interval makes that restart a
non-event, which is the same soft-state property the broker already claims for
placements.

Deliberately minimal payload. The node announces only what the broker cannot
learn by itself — where to reach it — and the broker discovers units, devices
and residency by snapshotting the facade it was just handed. Announcing more
would create a second copy of a fact that already has an owner.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Callable, Optional

DEFAULT_BROKER_URL = "http://127.0.0.1:8799"
DEFAULT_INTERVAL_S = 30.0
# Retry cadence while the broker is unreachable. Capped so a node that starts
# before its broker does not spin, and does not wait long once it appears.
RETRY_MIN_S = 2.0
RETRY_MAX_S = 60.0


def broker_url() -> str:
    return (os.environ.get("LIVESTACK_BROKER_URL") or DEFAULT_BROKER_URL).rstrip("/")


def facade_answers(facade_url: str, timeout: float = 2.0) -> bool:
    """Does the facade this node is about to announce actually answer?

    `attach()` starts the registrar thread at import — before uvicorn binds, and
    long before the model is loadable. A server that never binds must not claim
    duty, so the node checks its own front door before telling the broker about
    it. This is the node half of "an announce registers, only a snapshot
    certifies" (see `membership.py`): the broker stopped trusting announces, and
    the node stops making ones it cannot back.

    Any failure — connection refused because the bind has not happened, a 503
    from a server that is up but not ready, a timeout — is False. The registrar
    then backs off and tries again; it never gives up, because start order is
    not something a fleet should have to arrange.
    """
    req = urllib.request.Request(f"{facade_url.rstrip('/')}/residence", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", resp.getcode()) < 300
    except Exception:
        return False


def register_once(facade_url: str, *, host_id: str, kind: str,
                  broker: Optional[str] = None, timeout: float = 3.0) -> dict:
    body = json.dumps({
        "facade_url": facade_url,
        "host_id": host_id,
        "kinds": [kind],
    }).encode()
    req = urllib.request.Request(
        f"{broker or broker_url()}/peers", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def start_registrar(facade_url: str, *, host_id: str, kind: str,
                    interval_s: float = DEFAULT_INTERVAL_S,
                    broker: Optional[str] = None,
                    log: Callable[[str], None] = print,
                    answers: Callable[[str], bool] = facade_answers,
                    register: Optional[Callable[..., dict]] = None
                    ) -> threading.Thread:
    """Register now, then renew forever. Daemon thread: dies with the process,
    which is correct — a node that has exited should stop claiming duty, and
    the broker's own aging turns that silence into MIA.

    `answers` and `register` are injection points for tests; production uses the
    module-level defaults."""
    register = register or register_once

    def _loop():
        backoff = RETRY_MIN_S
        announced = False
        while True:
            # Self-probe FIRST. The thread starts at import, so on a cold boot
            # this fails for as long as the server takes to bind — during which
            # the node says nothing rather than announcing a door that is not
            # there. Same backoff ladder as an unreachable broker.
            if not answers(facade_url):
                if announced:
                    log(f"[livestack] facade stopped answering; withholding "
                        f"registration until {facade_url} is back")
                    announced = False
                _stop.wait(backoff)
                backoff = min(backoff * 2, RETRY_MAX_S)
                if _stop.is_set():
                    return
                continue
            try:
                register(facade_url, host_id=host_id, kind=kind, broker=broker)
                if not announced:
                    log(f"[livestack] reported for duty at {broker or broker_url()} "
                        f"as {facade_url}")
                    announced = True
                backoff = RETRY_MIN_S
                delay = interval_s
            except Exception as e:
                # Log the FIRST failure after a success, not every retry — a
                # broker that is down for an hour is one event, not 120 lines.
                if announced:
                    log(f"[livestack] broker unreachable, will keep renewing: {e}")
                    announced = False
                delay = backoff
                backoff = min(backoff * 2, RETRY_MAX_S)
            _stop.wait(delay)
            if _stop.is_set():
                return

    _stop = threading.Event()
    t = threading.Thread(target=_loop, name="livestack-registrar", daemon=True)
    t.stop = _stop.set  # type: ignore[attr-defined]
    t.start()
    return t
