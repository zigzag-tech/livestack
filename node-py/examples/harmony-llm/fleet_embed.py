"""Embed nodes on the mesh: where a `/v1/embeddings` need goes when no unit of
THIS node meets it.

The gateway's admission is for LOADING a model onto a card. A CPU/Vulkan embed
node (livestack_node.embedding; the fleet runs EmbeddingGemma 2 only) loads nothing the
planner arbitrates and serves from its own process, so there is nothing to
admit: the question is only "which live embed node declares what the caller
asked for?", and the broker's `/fleet` view already answers it. Callers state
the need (`require:class=embed,dim=512,multilingual=true`) and never learn a
node's address; before this a need no local unit met was a 503 even though a
node on the mesh met it.

`attrs_meet` is the one comparator for both this and `_local_satisfies`, so a
clause cannot mean one thing for a local unit and another for a fleet node.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Iterable, Mapping, Optional

from livestack_node.requirements import attrs_meet  # noqa: F401  (re-exported: the one comparator)

def embed_nodes(fleets: Iterable[Mapping]) -> "list[dict]":
    """Live embed units across broker `/fleet` views: [{base, model, attrs,
    probe_ms, node}]. A node counts when it is `fresh`, serves kind `embed`,
    and reports `ready`; a unit with no attributes cannot meet any clause and
    is skipped."""
    out: "list[dict]" = []
    seen: set = set()
    for fleet in fleets:
        for host in (fleet.get("hosts") or {}).values():
            for n in host.get("nodes") or []:
                if n.get("state") != "fresh" or "embed" not in (n.get("kinds") or []) or not n.get("ready"):
                    continue
                peer = str(n.get("peer") or "")
                base = peer.rsplit("/livestack", 1)[0]
                if not base:
                    continue
                for u in n.get("units") or []:
                    attrs = u.get("attributes") or {}
                    if not attrs.get("model") or (base, attrs["model"]) in seen:
                        continue
                    seen.add((base, attrs["model"]))
                    out.append({"base": base, "model": attrs["model"], "attrs": dict(attrs),
                                "probe_ms": n.get("probe_ms"), "node": u.get("kind") or base})
    return out


class AmbiguousEmbedder(Exception):
    """The need is met by embedders of different MODELS and does not name one."""

    def __init__(self, models):
        self.models = sorted(models)
        super().__init__(
            f"{len(self.models)} embedding models meet this need ({', '.join(self.models)}); "
            "vectors from different models are not comparable, so name one with `model=<id>`")


def pick_all(requires: Mapping, nodes: Iterable[Mapping]) -> "list[dict]":
    """Every node whose unit meets `requires`, nearest first (lowest broker
    probe; a node that never reported one sorts last, not first).

    An embedding space is a data contract: when the satisfying nodes serve
    different models, choosing among them by distance would hand one caller a
    different space on different days. That is `AmbiguousEmbedder`, not a pick.
    Several nodes serving the SAME model are interchangeable, so a caller may
    fall through the list when the nearest one is busy."""
    ok = [n for n in nodes if attrs_meet(n["attrs"], requires)]
    models = {n["model"] for n in ok}
    if len(models) > 1:
        raise AmbiguousEmbedder(models)
    return sorted(ok, key=lambda n: (n.get("probe_ms") is None, n.get("probe_ms") or 0.0))


def pick(requires: Mapping, nodes: Iterable[Mapping]) -> Optional[dict]:
    """The nearest node whose unit meets `requires`, or None."""
    return next(iter(pick_all(requires, nodes)), None)


def fetch_fleets(brokers: Iterable[str], get: Callable) -> "list[dict]":
    """`/fleet` of every broker that answers. All of them, not the first: the
    dispatch broker on the gateway's own host lists only that host's nodes,
    while the federated view lists the mesh."""
    out = []
    for base in brokers:
        try:
            out.append(get(f"{base}/fleet", timeout=3.0).json())
        except Exception:
            continue
    return out


class EmbedView:
    """The embed nodes the brokers currently list, shared by every request.

    Each request used to fetch `/fleet` itself with a 3 s timeout. A broker that
    answers in 3-4 s now and then (observed on xc-tower-ubuntu) dropped the
    node from that ONE request's view, which then fell through to local
    admission and answered `503 nothing satisfies` for a node that was fine --
    and a burst of N requests made the slow answer likelier, not less. So: one
    refresh at a time (single-flight), reused for `ttl` seconds, and when a
    refresh finds no embed node the last non-empty view is kept for `grace`
    seconds. A node that is really gone is dropped once the grace is over; its
    own dead-node 502 covers the gap."""

    def __init__(self, ttl: float = 5.0, grace: float = 60.0):
        self.ttl, self.grace = ttl, grace
        self.nodes: "list[dict]" = []
        self.fetched = 0.0           # when `nodes` was last refreshed
        self.last_good = 0.0         # when `nodes` was last non-empty
        self._lock: "asyncio.Lock | None" = None

    async def get(self, fetch: Callable[[], "list[dict]"]) -> "list[dict]":
        if self._lock is None:
            self._lock = asyncio.Lock()
        if time.monotonic() - self.fetched < self.ttl:
            return self.nodes
        async with self._lock:
            now = time.monotonic()
            if now - self.fetched < self.ttl:      # another request refreshed while we waited
                return self.nodes
            fresh = embed_nodes(await asyncio.get_running_loop().run_in_executor(None, fetch))
            if fresh:
                self.nodes, self.last_good = fresh, time.monotonic()
            elif time.monotonic() - self.last_good >= self.grace:
                self.nodes = []
            self.fetched = time.monotonic()
            return self.nodes
