#!/usr/bin/env python3
"""explain_reload.py — attribute the last reload of a kind, from the ledger alone.

    python scripts/explain_reload.py <ledger.jsonl> <kind>

A "reload" is an `evict` record for the kind followed by a `load` record for
the same kind. The ledger now carries `caused_by` on both (the owner whose
request needed the room; the owner whose request brought the unit back, or
"pressure" for a rule-0 shed), so the question the 27B thrash of 2026-09-19
could only answer from journal timestamps — WHOSE work kept reloading this
model — is answerable from the decision ledger by itself.

Exit 0 and the owner pair printed: the reload is fully attributed. Exit 1: no
evict (or no load after it) for the kind — either the unit was never evicted,
or it was evicted and never came back, and both are answers worth printing
loudly rather than silently.

Run from the node-py directory (or anywhere, with livestack_node importable):
    cd node-py && python scripts/explain_reload.py ~/.cache/livestack/decisions-xc-tower-ubuntu.jsonl llm_title
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Tuple

from livestack_node.ledger import JsonlLedger


def find_last_reload(records: List[dict], kind: str) -> Optional[Tuple[dict, dict]]:
    """The most recent (evict, load) pair for `kind`, in ledger order.

    The load must come AFTER the evict — a load with no preceding evict is a
    first start, not a reload. The NEXT evict after that load closes nothing
    here; this returns the last COMPLETED reload, which is the one a reader
    asking "who reloaded it last" means.
    """
    evicts = [r for r in records
              if r.get("decision") == "evict" and r.get("kind") == kind]
    if not evicts:
        return None
    ev = evicts[-1]
    after = [r for r in records
             if r.get("decision") == "load" and r.get("kind") == kind
             and (r.get("ts") or 0) > (ev.get("ts") or 0)]
    if not after:
        return (ev, None)  # evicted, never reloaded — an answer, printed as such
    return (ev, after[0])


def _caused_by(rec: Optional[dict]) -> str:
    if rec is None:
        return "(none)"
    req = rec.get("request") or {}
    return req.get("caused_by") or "(unattributed — record predates caused_by)"


def explain(ledger_path: str, kind: str, out=sys.stdout) -> int:
    records = JsonlLedger(ledger_path).read()
    pair = find_last_reload(records, kind)
    if pair is None:
        print(f"{kind}: never evicted in this ledger — nothing to attribute.",
              file=out)
        return 1
    ev, load = pair
    if load is None:
        print(f"{kind}: evicted at ts={ev.get('ts')} (caused_by "
              f"{_caused_by(ev)}) and NEVER RELOADED — the fleet lost it.",
              file=out)
        return 1
    print(f"{kind} reload, from the ledger alone:", file=out)
    print(f"  evict  ts={ev.get('ts')}  caused_by={_caused_by(ev)}", file=out)
    print(f"  load   ts={load.get('ts')}  caused_by={_caused_by(load)}", file=out)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger", help="path to a decision-ledger JSONL file")
    ap.add_argument("kind", help="unit kind, e.g. llm_title")
    args = ap.parse_args(argv)
    return explain(args.ledger, args.kind)


if __name__ == "__main__":
    sys.exit(main())
