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
import time
import urllib.error
import urllib.parse
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


def admit(kind: str = "", *, requires: Optional[dict] = None, owner_id: str = "node",
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

    Degradation is deliberate and NARROW: only when no broker ANSWERS does this
    return `granted: True, device_id: None, degraded: <why>`, meaning "proceed
    as if arbitration did not exist". An arbitration outage must not take a
    model offline.

    A broker that answers and does not grant is a REFUSAL — `granted: False`
    with a `reason` — and a caller that loads anyway defeats the point. That
    distinction was missing on 2026-09-07: a node registered seconds earlier was
    not yet in the planner's world, the broker turned its own KeyError into
    `granted: True`, and the node ran vLLM into a card that still held a 22 GB
    model. Refusal and outage must not look alike.
    """
    from .announce import broker_urls
    # No device selector: WHERE it goes is the planner's decision, and pinning it
    # here would be the caller deciding placement again. `requires` goes one
    # further — WHICH model is also the planner's decision, from a stated need
    # ({"class": "llm", "params_b>": 7, "params_b<=": 10}). The answer carries
    # `kind`, because a caller that asked for a capability has no other way to
    # know what it got.
    body = {"kind": kind, "owner": owner_id}
    if requires:
        body["requires"] = dict(requires)
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


class NoEligibleTarget(RuntimeError):
    """Nothing the fleet offered satisfied this caller's policy.

    Carries every rejection with its reason. A picker that returned `None`
    would make "the fleet has no TTS" and "every TTS is in the wrong region"
    the same event, and those want opposite responses: the first is an outage,
    the second is a correct refusal.
    """

    def __init__(self, kind: str, rejected: list):
        self.kind = kind
        self.rejected = rejected
        detail = "; ".join(f"{r['target_id']} ({r['why']})" for r in rejected[:6]) or "no candidates at all"
        super().__init__(f"no eligible {kind} target: {detail}")


def rank_snapshot(kind: str, *, brokers: Optional[list] = None,
                  vantage: Optional[str] = None, asker_region: Optional[str] = None,
                  timeout: float = 3.0) -> dict:
    """Ask a broker where a `kind` request should start. First broker that answers.

    The brokers are tried in order because the first is this node's own host
    broker by convention, and its view is the one whose distances were measured
    from here.
    """
    from .announce import broker_urls

    query = {"kind": kind}
    if vantage:
        query["via"] = vantage
    if asker_region:
        # Recorded by the broker, never applied by it — policy is the caller's.
        # Sending it is what makes the decision ledger readable afterwards.
        query["region"] = asker_region
    qs = urllib.parse.urlencode(query)

    last = None
    for base in (brokers if brokers is not None else broker_urls()):
        try:
            with urllib.request.urlopen(f"{base.rstrip('/')}/fleet/rank?{qs}", timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            continue
    raise NoEligibleTarget(kind, [{"target_id": "(broker)", "why": f"no broker answered ({last})"}])


def eligible_targets(ranking: dict, *, allow_regions=None,
                     allow_unknown_region: bool = False,
                     now: Optional[float] = None) -> tuple:
    """Apply the caller's policy to a ranking. Pure, so the policy is testable.

    Returns `(kept, rejected)`, both in the broker's own order, each rejected
    row carrying why it went.

    Two rules, and both are refusals rather than preferences:

    * **The TTL is enforced, not softened.** `fleet_rank` says a stale ranking
      is worse than none, because none falls back to a working default while
      stale looks authoritative. Past the TTL nothing is eligible.
    * **Unknown region is excluded by default.** A node that has not said where
      it is has not said it is here. The alternative — treating silence as a
      match — is how one unlabelled node in the wrong country quietly becomes
      the nearest thing the caller will accept.
    """
    now = time.time() if now is None else now
    generated_at = ranking.get("generated_at")
    ttl_s = ranking.get("ttl_s") or 0.0
    targets = list(ranking.get("targets") or [])
    targets.sort(key=lambda t: (t.get("rank") is None, t.get("rank") or 0))

    if generated_at is not None and ttl_s and now - float(generated_at) > float(ttl_s):
        age = now - float(generated_at)
        return [], [{"target_id": t.get("target_id", "?"),
                     "why": f"ranking is {age:.0f}s old, past its {ttl_s:.0f}s ttl"}
                    for t in targets] or [{"target_id": "(ranking)",
                                           "why": f"ranking is {age:.0f}s old, past its {ttl_s:.0f}s ttl"}]

    wanted = {r.strip().lower() for r in allow_regions} if allow_regions else None
    kept, rejected = [], []
    for t in targets:
        region = (t.get("region") or "").strip().lower() or None
        if wanted is not None:
            if region is None and not allow_unknown_region:
                rejected.append({"target_id": t.get("target_id", "?"),
                                 "why": "no region declared"})
                continue
            if region is not None and region not in wanted:
                rejected.append({"target_id": t.get("target_id", "?"),
                                 "why": f"region {region}, wanted {'/'.join(sorted(wanted))}"})
                continue
        kept.append(t)
    return kept, rejected


def choose(kind: str, *, allow_regions=None, allow_unknown_region: bool = False,
           brokers: Optional[list] = None, vantage: Optional[str] = None,
           asker_region: Optional[str] = None, timeout: float = 3.0,
           now: Optional[float] = None) -> dict:
    """The endpoint to send a `kind` request to, under this caller's policy.

    This is the half of placement the fleet broker deliberately does not do.
    It ranks by measured distance and records the asker's region without
    applying it, because "which regions may this account use" is policy and a
    broker that decided policy would be a second place for it to be wrong. So
    the caller states the policy, here, once — and every consumer gets the same
    enforcement instead of each writing its own host list.

    Raises `NoEligibleTarget` rather than falling back to something outside the
    policy. A caller that would rather have a distant engine than none says so
    by widening `allow_regions`.
    """
    ranking = rank_snapshot(kind, brokers=brokers, vantage=vantage,
                            asker_region=asker_region, timeout=timeout)
    kept, rejected = eligible_targets(ranking, allow_regions=allow_regions,
                                      allow_unknown_region=allow_unknown_region, now=now)
    if not kept:
        raise NoEligibleTarget(kind, rejected)
    best = kept[0]
    return {
        "endpoint": best.get("target_id"),
        "node": best.get("node"),
        "host_id": best.get("host_id"),
        "region": best.get("region"),
        "distance_ms": best.get("distance_ms"),
        "distance_band": best.get("distance_band"),
        # What the broker would have picked with no policy, so a caller can see
        # when policy changed the answer — and a ledger can record both.
        "broker_choice": ranking.get("chosen"),
        "reason": best.get("reason") or ranking.get("reason"),
        "rejected": rejected,
    }
