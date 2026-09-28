"""compose.py — run a composition decision against live node facts, record it,
and later join what actually happened to it. Proposes; never applies.

    python -m livestack_node.compose                 # propose, record, print the diff
    python -m livestack_node.compose --outcomes      # join outcomes to past proposals
    python -m livestack_node.compose --replay <decision_id>

The pure parts live in `composition.py` (feasible, cost, composer) and
`composition_replay.py` (the admission model). This module is the loop around
them: read each harmony-llm node's `/composition/facts`, build a
`CompositionState`, run the composer, write ONE ledger decision with the facts
stored as its snapshot (so the decision can be re-run, not only read), and
return the proposal as a diff of the units file a person would apply.

It writes to its own ledger file (`composition-<host>.jsonl`, same writer and
bounds as the decision ledger) rather than the host broker's, because the CLI
and the broker are separate processes and the writer's rotation is not
multi-process safe. See openspec change `harmony-placement-foundation`.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from . import composition as cm
from . import composition_replay as rp
from .ledger import Candidate, Decision, JsonlLedger, ledger_from_env
from .snapshots import SnapshotStore, snapshot_store_from_env
from .vllm_startup import GIB, CompositionKey, MeasuredCost

WEIGHTS_V1 = os.path.join(os.path.dirname(__file__), "composition_weights", "v1.json")
DEFAULT_FACTS = "http://127.0.0.1:8188/composition/facts"
DEFAULT_LENS = (16384, 24576, 32768)
DEFAULT_SEQS = (16, 32)
TRACE_S = 21 * 86400                 # the demand log's own retention
WINDOW_S, WINDOW_COUNT, WINDOW_STRIDE_S = 6 * 3600, 3, 86400
SERVED_HOURS = 24
NOT_APPLIED_AFTER_S = 7 * 86400


# --- facts -> state (pure) --------------------------------------------------------

def _key(row: Mapping) -> CompositionKey:
    c = row["composition"]
    return CompositionKey(base=c["base"], adapters=tuple(tuple(a) for a in c["adapters"]),
                          kv_dtype=c.get("kv_dtype") or "auto",
                          max_model_len=int(c.get("max_model_len") or 0),
                          max_num_seqs=int(c.get("max_num_seqs") or 0),
                          engine_version=c.get("engine_version") or "",
                          extra=tuple(c.get("extra") or ()))


def _cost(row: Mapping) -> MeasuredCost:
    f = {k: row[k] for k in MeasuredCost.__dataclass_fields__ if k in row}
    return MeasuredCost(**f)


def _live_unit(facts: Mapping) -> Optional[Mapping]:
    units = facts.get("units") or []
    return next((u for u in units if u.get("resident")), units[0] if units else None)


def device_id_of(facts: Mapping) -> str:
    return facts.get("device_id") or f"{facts.get('host_id')}:gpu"


def state_from_facts(facts_list: Sequence[Mapping], *, now: float,
                     lens: Sequence[int] = DEFAULT_LENS,
                     seqs: Sequence[int] = DEFAULT_SEQS) -> Tuple[cm.CompositionState, List[str]]:
    """Build the state a composition decision reads. Returns (state, notes):
    a node whose card size is unknowable is left out and NAMED in the notes,
    never sized at 0."""
    devices, adapters, bases, live, engine, pins, unit_bases = [], {}, set(), {}, {}, set(), {}
    measured: Dict[str, MeasuredCost] = {}
    keys: Dict[str, CompositionKey] = {}
    trace: List[dict] = []
    kv_all, notes = set(), []
    for facts in facts_list:
        dev = device_id_of(facts)
        rows = [r for r in facts.get("measured_rows") or [] if r.get("measured") != "unknown"
                and r.get("composition")]
        started = set()
        latest_engine = ""
        for r in sorted(rows, key=lambda r: r.get("measured_at") or 0):
            k = _key(r)
            h = r["composition_hash"]
            measured[h], keys[h] = _cost(r), k
            started |= cm.launch_flags(cm.Composition(dev, k.base, frozenset(a for a, _ in k.adapters),
                                                      k.kv_dtype, k.max_model_len, k.max_num_seqs))
            latest_engine = k.engine_version or latest_engine
        u = _live_unit(facts)
        if u is None:
            notes.append(f"{dev}: no units reported; left out")
            continue
        for unit in facts.get("units") or []:
            unit_bases[unit["name"]] = unit["model"]
            bases.add(unit["model"])
            for name, rank in (unit.get("adapters") or {}).items():
                adapters[name] = cm.Adapter(name, unit["model"], int(rank))
            # Adapters on disk that no unit loads yet: they apply to a unit
            # whose declared `lora_base` is the model they were trained on.
            for a in facts.get("adapter_catalogue") or []:
                if a.get("error"):
                    notes.append(f"{dev}: adapter {a.get('name')} unreadable: {a['error']}")
                    continue
                if unit.get("lora_base") and a.get("lora_base") == unit["lora_base"] \
                        and a["name"] not in adapters:
                    adapters[a["name"]] = cm.Adapter(a["name"], unit["model"], int(a["rank"]))
            if unit.get("residency") == "HARD_PIN":
                pins.add((dev, unit["model"]))
        # THE CARD AS THE ENGINE SEES IT. vLLM sizes its budget against the
        # memory CUDA exposes (23.56 GiB on a 3090), not nvidia-smi's total
        # (24.0 GiB). Using nvidia-smi's number handed the composer 0.42 GiB of
        # budget that does not exist, and on 2026-09-28 that alone made the
        # bf16 two-adapter composition, which vLLM refuses to start, look
        # feasible. So: budget / fraction from a measurement of this card,
        # whenever one exists; nvidia-smi only as a named fallback.
        capacity = None
        fraction = float(u.get("gpu_fraction") or 0)
        m = (u.get("measured") or {})
        if m.get("gpu_fraction"):
            fraction = float(m["gpu_fraction"])
        engine_seen = [r for r in rows if r.get("budget") and r.get("gpu_fraction")]
        if m.get("budget") and m.get("gpu_fraction"):
            capacity = int(m["budget"] / float(m["gpu_fraction"]))
        elif engine_seen:
            r = max(engine_seen, key=lambda r: r.get("measured_at") or 0)
            capacity = int(r["budget"] / float(r["gpu_fraction"]))
        elif facts.get("capacity_bytes"):
            capacity = int(facts["capacity_bytes"])
            notes.append(f"{dev}: card size from nvidia-smi ({capacity / GIB:.2f} GiB); the engine "
                         f"sees less, so predictions near the limit are optimistic until measured")
        if not capacity or not fraction:
            notes.append(f"{dev}: card size or budget fraction unknown; left out (never sized at 0)")
            continue
        devices.append(cm.DeviceSpec(dev, int(capacity), fraction))
        live[dev] = cm.Composition(dev, u["model"], frozenset(u.get("adapters") or {}),
                                   u.get("kv_dtype") or "auto", int(u.get("max_model_len") or 0),
                                   int(u.get("max_num_seqs") or 0))
        kv = tuple(facts.get("kv_dtypes") or ("auto",))
        kv_all |= set(kv)
        engine[dev] = cm.EngineFacts(latest_engine, frozenset(kv), frozenset(started))
        trace += list(facts.get("trace") or [])
    live_lens = {c.max_model_len for c in live.values()}
    live_seqs = {c.max_num_seqs for c in live.values()}
    state = cm.CompositionState(
        devices=tuple(devices), adapters=adapters, bases=tuple(sorted(bases)),
        measured=measured, measured_keys=keys,
        trace=tuple(sorted(trace, key=lambda r: r.get("ts") or 0)),
        live=live, engine=engine, hard_pins=frozenset(pins),
        search=cm.SearchSpace(tuple(sorted(kv_all or {"auto"})),
                              tuple(sorted(set(lens) | live_lens)),
                              tuple(sorted(set(seqs) | live_seqs))),
        unit_bases=unit_bases,
        windows=tuple(rp.past_windows(now, WINDOW_S, WINDOW_COUNT, WINDOW_STRIDE_S)),
        now=now)
    return state, notes


def units_diff(facts_list: Sequence[Mapping], chosen: Optional[cm.Composition]) -> Optional[dict]:
    """The chosen composition as edits to the units file a person applies."""
    if chosen is None:
        return None
    facts = next((f for f in facts_list if device_id_of(f) == chosen.device), None)
    u = _live_unit(facts) if facts else None
    if u is None:
        return {"device": chosen.device, "error": "no live unit on that device"}
    paths = {}
    for f in facts_list:
        for a in f.get("adapter_catalogue") or []:
            if a.get("path"):
                paths[a["name"]] = a["path"]
        for unit in f.get("units") or []:
            paths.update(unit.get("adapter_paths") or {})
    changes = []
    if set(u.get("adapters") or {}) != set(chosen.adapters):
        changes.append({"field": "adapters",
                        "from": {n: (u.get("adapter_paths") or {}).get(n) for n in sorted(u.get("adapters") or {})},
                        "to": {n: paths.get(n, "PATH UNKNOWN") for n in sorted(chosen.adapters)}})
    args = (u.get("extra_args") or "").split()

    def set_flag(flag: str, value: Optional[str]) -> None:
        nonlocal args
        out, skip = [], False
        for i, a in enumerate(args):
            if skip:
                skip = False
                continue
            if a == flag:
                skip = True
                continue
            if a.startswith(flag + "="):
                continue
            out.append(a)
        if value is not None:
            out += [flag, value]
        args = out

    if (u.get("kv_dtype") or "auto") != chosen.kv_dtype:
        set_flag("--kv-cache-dtype", None if chosen.kv_dtype == "auto" else chosen.kv_dtype)
    if int(u.get("max_num_seqs") or 0) != chosen.max_num_seqs:
        set_flag("--max-num-seqs", str(chosen.max_num_seqs))
    new_args = " ".join(args)
    if new_args != (u.get("extra_args") or ""):
        changes.append({"field": "extra_args", "from": u.get("extra_args") or "", "to": new_args})
    if int(u.get("max_model_len") or 0) != chosen.max_model_len:
        changes.append({"field": "max_model_len", "from": str(u.get("max_model_len")),
                        "to": str(chosen.max_model_len)})
    return {"unit": u["name"], "device": chosen.device, "changes": changes}


# --- the run (I/O) ----------------------------------------------------------------

def fetch_facts(urls: Sequence[str], since: float, timeout: float = 30.0) -> List[dict]:
    out = []
    for url in urls:
        sep = "&" if "?" in url else "?"
        with urllib.request.urlopen(f"{url}{sep}since={since:.0f}", timeout=timeout) as r:
            out.append(json.load(r))
    return out


def _decision_record(dec: cm.CompositionDecision, *, emitter_id: str, snapshot: Optional[str],
                     now: float) -> Decision:
    cands = []
    for r in dec.candidates:
        total = r.cost.total if r.cost else None
        cands.append(Candidate(
            id=r.hash, outcome=r.outcome, device_id=r.device,
            reason=(r.reason if r.reason else f"cost {total:.1f}" if total is not None else "unscored"),
            detail={"composition": r.composition.to_json(), "live": r.live,
                    "feasibility": r.feasibility,
                    "cost": r.cost.to_json() if r.cost else None,
                    "prediction": r.prediction.to_json() if r.prediction else None}))
    return Decision(emitter="composition", emitter_id=emitter_id, decision="compose",
                    kind="llm", candidates=cands, chosen=dec.chosen_hash, reason=dec.reason,
                    # The decider and weights ride in `request` (what was asked
                    # of whom): the ledger's `policy` field is reserved for the
                    # scheduler's compiled-policy pointer and is schema-closed.
                    request={**dict(dec.policy), "candidates_total": dec.candidates_total,
                             "filtered": dict(dec.filtered)},
                    snapshot=snapshot, ts=now)


def propose(facts_list: Sequence[Mapping], *, ledger: Optional[JsonlLedger],
            store: Optional[SnapshotStore], weights_path: str = WEIGHTS_V1,
            emitter_id: str = "composition", now: Optional[float] = None,
            params: Optional[dict] = None) -> dict:
    now = time.time() if now is None else now
    params = dict(params or {"lens": list(DEFAULT_LENS), "seqs": list(DEFAULT_SEQS)})
    state, notes = state_from_facts(facts_list, now=now, lens=params["lens"], seqs=params["seqs"])
    weights = cm.load_weights(weights_path)
    dec = cm.run_composition(state, cm.ExhaustiveComposer(), weights)
    snapshot = (store.put_payload({"facts": list(facts_list), "now": now, "params": params,
                                   "weights": weights.hash})
                if store is not None else None)
    rec = _decision_record(dec, emitter_id=emitter_id, snapshot=snapshot, now=now)
    written = ledger.append(rec) if ledger is not None else None
    return {"decision_id": rec.decision_id, "recorded": written is not None,
            "snapshot": snapshot, "notes": notes, "decision": dec.to_json(),
            "units_diff": units_diff(facts_list, dec.chosen)}


def replay(decision_row: Mapping, store: SnapshotStore, weights_path: str = WEIGHTS_V1) -> dict:
    """Re-run a recorded composition decision from its snapshot."""
    payload = store.load_payload(decision_row["snapshot"])
    state, _ = state_from_facts(payload["facts"], now=payload["now"],
                                lens=payload["params"]["lens"], seqs=payload["params"]["seqs"])
    dec = cm.run_composition(state, cm.ExhaustiveComposer(), cm.load_weights(weights_path))
    same = (dec.chosen_hash == decision_row.get("chosen")
            and payload.get("weights") == (decision_row.get("request") or {}).get("weights"))
    return {"decision_id": decision_row.get("decision_id"), "reproduced": same,
            "recorded": decision_row.get("chosen"), "replayed": dec.chosen_hash}


# --- outcomes ------------------------------------------------------------------------

def _pctl(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def outcomes_for(rows: Sequence[Mapping], facts_list: Sequence[Mapping], now: float,
                 emitter_id: str = "composition") -> List[Decision]:
    """Outcome rows owed to past proposals (design §8), each exactly once:
    `measured` when the chosen composition first reports a measurement,
    hourly `served` for 24 h after that, `not_applied` after 7 days without."""
    done = {(r.get("parent_decision_id"), (r.get("outcome") or {}).get("kind"),
             (r.get("outcome") or {}).get("hour")) for r in rows if r.get("parent_decision_id")}
    measured = {}
    trace: List[Mapping] = []
    for f in facts_list:
        for m in f.get("measured_rows") or []:
            if m.get("composition_hash"):
                measured.setdefault(m["composition_hash"], []).append(m)
        trace += list(f.get("trace") or [])
    out: List[Decision] = []
    for d in rows:
        if d.get("decision") != "compose" or d.get("parent_decision_id"):
            continue
        chosen = d.get("chosen")
        if not chosen or chosen == "keep":
            continue
        did, ts = d["decision_id"], float(d.get("ts") or 0)
        seen = sorted((m for m in measured.get(chosen, []) if float(m.get("measured_at") or 0) >= ts),
                      key=lambda m: m.get("measured_at") or 0)
        mk = lambda outcome: Decision(emitter="composition", emitter_id=emitter_id,
                                      decision="compose", kind="llm", chosen=chosen,
                                      parent_decision_id=did, outcome=outcome,
                                      reason=f"outcome:{outcome['kind']}", ts=now)
        if not seen:
            if now - ts > NOT_APPLIED_AFTER_S and (did, "not_applied", None) not in done:
                out.append(mk({"status": "unknown", "kind": "not_applied", "recorded_at": now}))
            continue
        m = seen[0]
        if (did, "measured", None) not in done:
            pred = next(((c.get("detail") or {}).get("prediction") for c in d.get("candidates") or []
                         if c.get("id") == chosen), None) or {}
            meas = {"weights_gib": m["weights_nontorch"] / GIB, "activation_gib": m["peak_activation"] / GIB,
                    "cuda_graphs_gib": m["cuda_graphs"] / GIB, "kv_gib": m["kv_bytes"] / GIB,
                    "kv_tokens": m["kv_tokens"]}
            err = {k: round(meas[k] - pred[k], 4) for k in meas if isinstance(pred.get(k), (int, float))}
            out.append(mk({"status": "ok", "kind": "measured", "recorded_at": now,
                           "measured_at": m.get("measured_at"),
                           "predicted": {k: pred.get(k) for k in meas},
                           "measured": {k: round(v, 4) for k, v in meas.items()},
                           "error": err}))
        t0 = float(m.get("measured_at") or ts)
        for hour in range(SERVED_HOURS):
            lo, hi = t0 + hour * 3600, t0 + (hour + 1) * 3600
            if hi > now or (did, "served", hour) in done:
                continue
            recs = [r for r in trace if r.get("composition_hash") == chosen and lo <= float(r.get("ts") or 0) < hi]
            el = [float(r["elapsed_ms"]) for r in recs if r.get("elapsed_ms") is not None]
            out.append(mk({"status": "ok", "kind": "served", "hour": hour, "recorded_at": now,
                           "requests": len(recs),
                           "ok": sum(1 for r in recs if r.get("outcome") == "ok"),
                           "refused": sum(1 for r in recs if r.get("outcome") != "ok"),
                           "p50_ms": _pctl(el, 0.5), "p95_ms": _pctl(el, 0.95)}))
    return out


def _ledger_rows(ledger: JsonlLedger) -> List[dict]:
    rows = []
    for p in [ledger.path] + [f"{ledger.path}.{i}" for i in range(1, ledger.max_files)]:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
        except FileNotFoundError:
            pass
    return rows


def join_outcomes(ledger: JsonlLedger, facts_list: Sequence[Mapping], now: Optional[float] = None,
                  emitter_id: str = "composition") -> int:
    now = time.time() if now is None else now
    n = 0
    for rec in outcomes_for(_ledger_rows(ledger), facts_list, now, emitter_id):
        if ledger.append(rec) is not None:
            n += 1
    return n


# --- entry points ------------------------------------------------------------------------

def from_env():
    host = os.environ.get("LIVESTACK_HOST_ID", "").strip() or os.uname().nodename
    name = f"composition-{host}"
    say = lambda m: print(m, file=sys.stderr, flush=True)
    urls = [u.strip() for u in os.environ.get("LIVESTACK_COMPOSITION_FACTS", DEFAULT_FACTS).split(",")
            if u.strip()]
    return name, urls, ledger_from_env(name, 8, log=say), snapshot_store_from_env(name, log=say)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Propose a composition (never applies).")
    ap.add_argument("--facts", action="append", help="harmony-llm /composition/facts URL (repeatable)")
    ap.add_argument("--outcomes", action="store_true", help="join outcomes to past proposals")
    ap.add_argument("--replay", metavar="DECISION_ID")
    ap.add_argument("--no-record", action="store_true", help="print only; write no ledger row")
    a = ap.parse_args(argv)
    name, urls, ledger, store = from_env()
    urls = a.facts or urls
    now = time.time()
    if a.replay:
        row = next((r for r in _ledger_rows(ledger) if r.get("decision_id") == a.replay), None)
        if row is None:
            print(f"no decision {a.replay} in {ledger.path}", file=sys.stderr)
            return 2
        out = replay(row, store)
        print(json.dumps(out, indent=2))
        return 0 if out["reproduced"] else 1
    facts = fetch_facts(urls, since=now - TRACE_S)
    if a.outcomes:
        print(json.dumps({"outcome_rows_written": join_outcomes(ledger, facts, now, name)}))
        return 0
    out = propose(facts, ledger=None if a.no_record else ledger,
                  store=None if a.no_record else store, emitter_id=name, now=now)
    brief = {k: out[k] for k in ("decision_id", "recorded", "snapshot", "notes", "units_diff")}
    brief["chosen"] = out["decision"]["chosen"]
    brief["reason"] = out["decision"]["reason"]
    brief["candidates_total"] = out["decision"]["candidates_total"]
    brief["filtered"] = out["decision"]["filtered"]
    print(json.dumps(brief, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
