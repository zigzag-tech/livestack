"""fleet_pools.py — the elastic pools the fleet may burst into, as configuration.

``fleet_admit.targets_from_view`` builds the targets that ALREADY EXIST, from the
fleet view. It has never built the ones that could exist, which is why
``schedule()`` — a function perfectly capable of emitting ``Provision`` — has
never emitted one in production: with no elastic target in its ``FleetState``
there was nothing to provision onto.

A pool is an operator's statement: *here is a shape of machine I am willing to
rent, in this region, at this price, up to this many at once.* Livestack cannot
discover that. Price and willingness are not properties of a cloud API.

The declaration is one environment variable of JSON, for the same reason the
account quota is: it is an operator decision, it must be visible in a unit file,
and a typo in it must degrade LOUDLY rather than quietly removing the fleet's
ability to burst. A malformed value yields NO pools and says so at startup —
never a partially-parsed one, because half a pool list is a fleet that bursts
into the wrong region at the wrong price.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .fleet_scheduler import CostModel, Target, Tier
from .fleet_workers import WorkerSpec

#: What one instance of a pool is assumed able to serve concurrently. Same
#: meaning as `fleet_admit.DEFAULT_CONCURRENCY`, and the same caveat: it is a
#: CONCURRENCY ceiling, not a memory one.
DEFAULT_CONCURRENCY = 4.0
#: How long a burst instance takes to become usable. Used by the scheduler's
#: deadline feasibility test, so it must be an honest number rather than an
#: optimistic one: understating it makes the planner promise deadlines the fleet
#: then misses, which is worse than declining them.
DEFAULT_PROVISION_LATENCY_S = 240.0


@dataclass(frozen=True)
class Pool:
    """One rentable shape, priced. ``kinds`` is what it can serve; empty means
    "anything", which is the right default for a general worker image."""
    id: str
    provider: str
    tier: Tier
    region: str
    instance_type: str
    kinds: Tuple[str, ...] = ()
    concurrency: float = DEFAULT_CONCURRENCY
    cost_per_hour: float = 0.0
    provision_latency_s: float = DEFAULT_PROVISION_LATENCY_S
    max_instances: int = 1
    image_id: Optional[str] = None
    image_family: str = "ubuntu_24_04_x64"
    system_disk_category: str = "cloud_essd"
    system_disk_gib: int = 80
    auto_release_hours: float = 4.0
    bootstrap: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


def parse_pools(raw: str, *, log: Callable[[str], None] = lambda *_: None
                ) -> Tuple[Pool, ...]:
    """Parse the pool declaration. All or nothing, and loud about it."""
    raw = (raw or "").strip()
    if not raw:
        return ()
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("expected a JSON list of pool objects")
        pools = tuple(_pool(r) for r in rows)
    except Exception as exc:  # noqa: BLE001 — operator input; report, never guess
        log(f"[fleet] LIVESTACK_FLEET_POOLS is malformed ({exc}); NO elastic pool "
            f"is configured, so the fleet cannot burst. Fix the value and "
            f"restart; GET /fleet reports the pools in force.")
        return ()
    ids = [p.id for p in pools]
    if len(set(ids)) != len(ids):
        log(f"[fleet] LIVESTACK_FLEET_POOLS has duplicate pool ids {ids}; NO "
            f"elastic pool is configured")
        return ()
    log(f"[fleet] {len(pools)} elastic pool(s): "
        + "; ".join(f"{p.id} {p.tier.name} {p.provider}/{p.region} "
                    f"{p.instance_type} ¥{p.cost_per_hour}/h x{p.max_instances}"
                    for p in pools))
    return pools


def _pool(row: Mapping[str, object]) -> Pool:
    tier = str(row.get("tier") or "SPOT").upper()
    if tier not in Tier.__members__:
        raise ValueError(f"tier {tier!r} is not one of {list(Tier.__members__)}")
    if not row.get("id") or not row.get("provider") or not row.get("region"):
        raise ValueError(f"pool needs id, provider and region: {row!r}")
    known = set(Pool.__dataclass_fields__) - {"tier", "kinds", "labels", "env"}
    unknown = set(row) - known - {"tier", "kinds", "labels", "env"}
    if unknown:
        # A misspelled key that is silently ignored is a pool that costs a
        # different amount than the unit file says it does.
        raise ValueError(f"pool {row.get('id')!r} has unknown key(s) {sorted(unknown)}")
    return Pool(
        tier=Tier[tier],
        kinds=tuple(str(k) for k in (row.get("kinds") or ())),
        labels={str(k): str(v) for k, v in (row.get("labels") or {}).items()},
        env={str(k): str(v) for k, v in (row.get("env") or {}).items()},
        **{k: v for k, v in row.items()
           if k in known})


def pool_targets(pools: Tuple[Pool, ...], *, kinds: Tuple[str, ...] = (),
                 running_instances: Optional[Mapping[str, int]] = None,
                 allow_regions: Tuple[str, ...] = (),
                 ) -> Tuple[Tuple[Target, ...], List[Dict[str, str]]]:
    """The elastic targets a plan may burst onto, and a ROW for every pool that
    could not be one, with the reason.

    The excluded rows are the point, not a by-product. "Nothing could be
    provisioned" and "every pool was excluded by your region policy" are the
    same outcome and opposite problems, and a plan that reports only the first
    sends its reader looking for capacity that was never the issue.
    """
    running = dict(running_instances or {})
    targets: List[Target] = []
    excluded: List[Dict[str, str]] = []
    wanted = {r.strip().lower() for r in allow_regions if r.strip()}
    for p in pools:
        # A pool that declares kinds must serve at least one of the kinds this
        # plan is about; one that declares none serves anything.
        if kinds and p.kinds and not set(kinds) & set(p.kinds):
            excluded.append({
                "pool_id": p.id,
                "reason": (f"does not serve {'/'.join(kinds)} "
                           f"(serves {'/'.join(p.kinds)})")})
            continue
        if wanted and (p.region or "").lower() not in wanted:
            excluded.append({"pool_id": p.id,
                             "reason": f"region {p.region}, caller allows {'/'.join(sorted(wanted))}"})
            continue
        up = int(running.get(p.id, 0))
        if up >= p.max_instances:
            excluded.append({"pool_id": p.id,
                             "reason": f"at its ceiling ({up} of {p.max_instances} up)"})
            continue
        targets.append(Target(
            id=p.id, host_id=p.id, tier=p.tier,
            capacity={"concurrency": p.concurrency},
            cost=CostModel(per_hour=p.cost_per_hour),
            provision_latency_s=p.provision_latency_s,
            running=False, elastic=True,
            max_instances=p.max_instances, running_instances=up,
            labels={"provider": p.provider, "region": p.region,
                    "instance_type": p.instance_type, "pool": p.id,
                    **dict(p.labels)},
            # An elastic pool has no measured distance — it does not exist yet.
            # `None` is scored as the worst measured candidate, which is the
            # honest reading: a machine that is not up cannot be near.
            distance_ms=None, utilization=None))
    return tuple(targets), excluded


def spec_for(pool: Pool, *, announce_env: Optional[Mapping[str, str]] = None
             ) -> WorkerSpec:
    """The worker spec one instance of ``pool`` should be created with."""
    return WorkerSpec(
        region=pool.region, instance_type=pool.instance_type,
        name_prefix=f"livestack-{pool.id}",
        image_id=pool.image_id, image_family=pool.image_family,
        system_disk_category=pool.system_disk_category,
        system_disk_gib=pool.system_disk_gib,
        auto_release_hours=pool.auto_release_hours,
        bootstrap=pool.bootstrap,
        announce_env={**dict(pool.env), **dict(announce_env or {})},
        labels=dict(pool.labels))
