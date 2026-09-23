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


def broker_urls() -> list:
    """Every broker this node reports for duty to.

    `LIVESTACK_BROKER_URL` is a COMMA LIST. One entry is the ordinary case — a
    node and its host broker on one machine — and two is the fleet case: the
    host broker that may warm and evict it, and the fleet broker that only wants
    to know it exists.

    Announcing to both is what lets the fleet broker's `LIVESTACK_PEERS` shrink
    back to its real meaning. A seed is an operator's statement that a node OUGHT
    to exist, so its absence is worth reporting; it was never meant to be how
    membership works, and with 0.1 and 0.2 in place an announce is discovery only
    and carries no claim about health.
    """
    raw = os.environ.get("LIVESTACK_BROKER_URL") or DEFAULT_BROKER_URL
    out = []
    for part in raw.split(","):
        part = part.strip().rstrip("/")
        if part and part not in out:
            out.append(part)
    return out or [DEFAULT_BROKER_URL]


def node_region() -> Optional[str]:
    """Which region this node is in, as the operator stated it.

    The broker cannot learn this. It can measure how far away a node is from
    one vantage — and it does, which is what `fleet_rank` orders on — but a
    measured distance answers "how far from here", never "where is it". Those
    are different questions the moment a second asker exists: zz-tower0 is 2 ms
    from a caller in Nanjing and 546 ms from one in Vaughan, and the fact that
    makes it ineligible for North-American work is neither of those numbers.
    It is where the machine is.

    So region is announced like `host_id`: a fact about the node that only the
    node's operator knows. `LIVESTACK_NODE_REGION` is free-form on purpose —
    the fleet's own vocabulary today is `na` and `cn`, and freezing an enum
    here would mean a code change to open an office.

    Unset is `None`, and a consumer filtering by region must treat unknown as
    excluded rather than as a match. A region we failed to learn is not
    evidence of nearness, for the same reason an unmeasured distance is not.
    """
    value = (os.environ.get("LIVESTACK_NODE_REGION") or "").strip().lower()
    return value or None


def node_operation_id() -> Optional[str]:
    """The provisioning operation this node was created for, or None.

    Set by the fleet broker in the instance's boot environment
    (`LIVESTACK_OPERATION_ID`) at the moment it calls the provider. A node that
    nobody provisioned has none, and that is the common case.

    Announced for one reason: it is the only thing that makes a create's success
    PROVABLE. The broker wrote the id before it spent the money; the node repeats
    it back once it is serving; the two are joined. Anything weaker — a new node
    appearing, a count going up — is a coincidence that happens to be true most
    of the time, which is the worst kind of evidence to bill against.
    """
    value = (os.environ.get("LIVESTACK_OPERATION_ID") or "").strip()
    return value or None


def node_scope() -> Optional[dict]:
    """Who this node is pooled FOR, as the operator or the enrolling hub
    stated it (`LIVESTACK_NODE_SCOPE`, a JSON object)::

        {"kind": "owner", "id": "sorbonne"}

    `kind` is `owner` | `org` | `realm`; `id` is the namespace. A node with a
    scope is a GRANT, announced like region: the broker cannot learn who a box
    belongs to by probing it, and a fleet that places another account's work
    on a self-scoped GPU has spent a resource its owner never offered. The
    admission path rejects an out-of-scope owner with the scope named.

    Matching is by namespace: an owner is admitted when it equals `id` or
    lives under it (`id` + `:` prefix) — `sorbonne` and `sorbonne:acct_1`
    both fit a scope of `sorbonne`. For `org` and `realm` the same rule
    applies to whatever id the grant names; the fleet does not yet resolve
    owners to organisations, so the grant speaks in owner namespaces.

    A malformed value is None, announced as no scope at all: a typo must not
    half-scope a node into a state nobody can reason about.
    """
    raw = (os.environ.get("LIVESTACK_NODE_SCOPE") or "").strip()
    if not raw:
        return None
    try:
        scope = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(scope, dict):
        return None
    kind = str(scope.get("kind") or "").strip().lower()
    ident = str(scope.get("id") or "").strip()
    if kind not in ("owner", "org", "realm") or not ident:
        return None
    return {"kind": kind, "id": ident}


def _scope_admits(scope: Optional[dict], owner: str) -> bool:
    """Does `scope` admit `owner`? No scope admits everyone (it is the
    default, and the fleet's pooled nodes); a scope admits owners in its
    namespace. Kept here, beside the parse, so the announce side and the
    admission side cannot drift on what a grant means."""
    if not isinstance(scope, dict):
        return True
    ident = str(scope.get("id") or "")
    if not ident:
        return True
    return owner == ident or owner.startswith(ident + ":")


def broker_url() -> str:
    """The FIRST broker, for callers that want one. Kept because a node's own
    host broker is the first entry by convention and some callers legitimately
    mean only that one."""
    return broker_urls()[0]


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
                  region: Optional[str] = None,
                  scope: Optional[dict] = None,
                  operation_id: Optional[str] = None,
                  broker: Optional[str] = None, timeout: float = 3.0) -> dict:
    """Announce to every configured broker. Raises only if ALL of them failed.

    All, not any: a node's own host broker is what arbitrates its card, and a
    fleet broker being down must not make the node look unregistered to it. The
    converse matters too — a node whose host broker is restarting should still
    reach the fleet — so the loop keeps going and the last error is re-raised
    only when nothing got through.
    """
    body = json.dumps({
        "facade_url": facade_url,
        "host_id": host_id,
        "kinds": [kind],
        # Omitted rather than sent as null when unset: an absent key leaves
        # whatever the broker already knew (a seed may carry one), while a null
        # would overwrite it with ignorance on every renewal.
        **({"region": region} if region else {}),
        # Who this node is pooled for. Announced only when set; unset is the
        # fleet default (a pooled node admits every owner) and must stay
        # invisible rather than announcing "no scope" over whatever a seed
        # carried.
        **({"scope": scope} if scope else {}),
        # Omitted when absent, like the two above: a node that was not
        # provisioned must not overwrite a recorded correlation with a null.
        **({"operation_id": operation_id} if operation_id else {}),
    }).encode()
    targets = [broker.rstrip("/")] if broker else broker_urls()
    out, last = {}, None
    for base in targets:
        req = urllib.request.Request(
            f"{base}/peers", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode() or "{}")
        except Exception as e:
            last = e
    if not out and last is not None:
        raise last
    return out


def start_registrar(facade_url: str, *, host_id: str, kind: str,
                    region: Optional[str] = None,
                    scope: Optional[dict] = None,
                    operation_id: Optional[str] = None,
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
                register(facade_url, host_id=host_id, kind=kind,
                         region=region, scope=scope,
                         operation_id=operation_id, broker=broker)
                if not announced:
                    log(f"[livestack] reported for duty at "
                        f"{broker or ', '.join(broker_urls())} as {facade_url}")
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
