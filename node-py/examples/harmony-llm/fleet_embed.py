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

from typing import Callable, Iterable, Mapping, Optional

CMPS = (">=", "<=", "!=", ">", "<")


def attrs_meet(attrs: Mapping, requires: Mapping) -> bool:
    """Same semantics as the planner's `_unit_satisfies`, including the
    important one: an attribute the unit does not declare is NOT satisfied.
    Silence is not a yes, or an unlabelled unit would answer every question."""
    for key, want in requires.items():
        op, attr = "", key
        for c in CMPS:
            if key.endswith(c):
                op, attr = c, key[: -len(c)]
                break
        attr = attr.strip()
        if attr not in attrs:
            return False
        have = attrs[attr]
        try:
            # A LIST want is "a value in this list" (the `context_len=[131072,]`
            # spelling) -- the same semantics as the planner's `_unit_satisfies`.
            if op == "" and isinstance(want, (list, tuple, set)):
                if have not in want:
                    return False
            elif op == "" and have != want:
                return False
            if op == "!=" and isinstance(want, (list, tuple, set)):
                if have in want:
                    return False
            if op == "!=" and not isinstance(want, (list, tuple, set)) and have == want:
                return False
            if op == ">=" and not float(have) >= float(want):
                return False
            if op == ">" and not float(have) > float(want):
                return False
            if op == "<=" and not float(have) <= float(want):
                return False
            if op == "<" and not float(have) < float(want):
                return False
        except (TypeError, ValueError):
            return False
    return True


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


def pick(requires: Mapping, nodes: Iterable[Mapping]) -> Optional[dict]:
    """The nearest node whose unit meets `requires` (lowest broker probe; a
    node that never reported one sorts last, not first).

    An embedding space is a data contract: when the satisfying nodes serve
    different models, choosing among them by distance would hand one caller a
    different space on different days. That is `AmbiguousEmbedder`, not a pick.
    Several nodes serving the SAME model are interchangeable, and the nearest wins."""
    ok = [n for n in nodes if attrs_meet(n["attrs"], requires)]
    models = {n["model"] for n in ok}
    if len(models) > 1:
        raise AmbiguousEmbedder(models)
    return min(ok, key=lambda n: (n.get("probe_ms") is None, n.get("probe_ms") or 0.0), default=None)


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
