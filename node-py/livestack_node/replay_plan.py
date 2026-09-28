"""replay_plan.py — re-run recorded `plan()` decisions from their snapshots.

    python -m livestack_node.replay_plan [--ledger PATH] [--since SECONDS]

Groups the ledger's plan action rows (evict/load/grant/defer) by the snapshot
they reference, reloads each snapshot, runs `plan()` on it with the recorded
policy, and compares the actions. A mismatch means the recorded state is not
the whole story (a nondeterminism, or an input the snapshot misses), which is
exactly what has to be known before any placement tuning trusts a replay.
Rows without a snapshot are counted separately: absence is not a match.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Iterable

from .planner import plan
from .snapshots import SnapshotStore, plan_signature

_PLAN_DECISIONS = {"evict", "load", "grant", "defer"}


def _rows(paths: Iterable[str]):
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except FileNotFoundError:
            continue


def replay(rows, store: SnapshotStore, since: float = 0.0) -> dict:
    groups = defaultdict(list)
    no_snapshot = 0
    for r in rows:
        if r.get("decision") not in _PLAN_DECISIONS or r.get("ts", 0) < since:
            continue
        if not r.get("snapshot"):
            no_snapshot += 1
            continue
        groups[r["snapshot"]].append(r)
    matched, mismatched, missing, details = 0, 0, 0, []
    for h, recs in groups.items():
        try:
            world, policy = store.load(h)
        except FileNotFoundError:
            missing += 1
            continue
        got = plan_signature(plan(world, policy))
        want = sorted(((r["decision"], r.get("kind"),
                        (r.get("candidates") or [{}])[0].get("device_id")
                        if r["decision"] in ("evict", "load") else None)
                       for r in recs), key=lambda t: tuple(str(x) for x in t))
        # Compare decision+kind (+device for evict/load); request ids live in
        # `request`, not in a stable column, so grants compare by kind.
        got_cmp = sorted(((d, k, dev if d in ("evict", "load") else None) for d, k, dev, _ in got),
                         key=lambda t: tuple(str(x) for x in t))
        if got_cmp == want:
            matched += 1
        else:
            mismatched += 1
            if len(details) < 20:
                details.append({"snapshot": h, "recorded": want, "replayed": got_cmp})
    return {"plans": len(groups), "matched": matched, "mismatched": mismatched,
            "snapshot_missing": missing, "rows_without_snapshot": no_snapshot,
            "mismatches": details}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    host = os.environ.get("LIVESTACK_HOST_ID", "").strip() or os.uname().nodename
    ap.add_argument("--name", default=f"decisions-{host}")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--snapshots", default=None)
    ap.add_argument("--since", type=float, default=3600.0, help="seconds back")
    a = ap.parse_args(argv)
    base = os.path.join(os.path.expanduser("~"), ".cache", "livestack")
    ledger = a.ledger or os.path.join(base, f"{a.name}.jsonl")
    store = SnapshotStore(a.snapshots or os.path.join(base, "snapshots", a.name))
    paths = [ledger] + [f"{ledger}.{i}" for i in range(1, 16)]
    out = replay(_rows(paths), store, since=time.time() - a.since)
    print(json.dumps(out, indent=2))
    return 1 if out["mismatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
