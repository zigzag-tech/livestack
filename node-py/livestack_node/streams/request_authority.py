"""`harmony.request/1`: a declarative residency request, answered as an intent (host or fleet scope).

The body carries the existing request language unchanged (docs/livestack-harmony.md, the
`harmony_requires` grammar): `require` and `prefer` map an attribute to one of four value shapes

    scalar      "llm", 27, true            equality
    any-of      ["qwen", "llama"]          a value in the list
    interval    "[20,30)"  "[20,]"  "(,30]" bounds, open or closed
    negation    {"not": "x"} {"not": [..]} !=

A key may also carry the planner's own comparison suffix (`params_b>=`), exactly as `harmony_requires`
does, and `adapter=<name>` means the unit serving that LoRA. Nothing here adds attributes: derived
attributes (`thinking`, `vision`, `tools`, `context_len`) are whatever the units already declare, and a
caller never supplies what Harmony can read from the request.

Two refusals, never confused (design D3):

* `requirement_inexpressible` -- a clause names an attribute NO unit in scope declares (the vocabulary
  is open: it is the union of the units' declared attributes), or a value shape the language cannot
  evaluate (a malformed interval, an interval over an attribute whose values are not numbers, an
  empty requirement, an unknown value shape). The partial carries `clause` (the attribute, `prefer.`
  prefixed for a prefer clause) and `detail` (what is wrong with it), so an operator who would have
  hand-edited `llm-units.json` finds the missing vocabulary named instead.
* `unsatisfiable` -- every clause was evaluable and no unit in scope meets them together.

Ladder `submitted < evaluated < placed < done`: `evaluated` names the best candidate unit (and host,
for fleet scope); `placed` when that unit is resident (carrying `lease` if the injected placer took
one); `done` once the answer stands -- immediately after `placed` when no lease was taken, or when the
lease is released. A request whose unit is not yet resident stays `evaluated` and the pump advances it.

Decision logic is NOT here. Candidate selection is the injected `candidates` callable; production wiring
(`hostd_streams`) passes the existing planner (`planner._unit_satisfies` + `candidate_kinds` ordering).
`reference_candidates` is a stdlib double for the lane and tests, pinned to the planner by a parity test.

Durability and bounds: the same `IntentLedger` as workloads, with this contract's catalog bounds:
`MAX_LIVE`=256 non-terminal rows, `MAX_BYTES`=1 MiB, terminal rows deleted after 7 days, unresolved
rows ended `expired` after 30 days.

Standard library only; relative imports only.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .common import (MAX_BODY_BYTES, REFUSAL_FORBIDDEN_FIELD, REFUSAL_INEXPRESSIBLE, REFUSAL_OVERSIZE,
                     REFUSAL_SCHEMA_INVALID, REFUSAL_STEP_NOT_ALLOWED, REFUSAL_UNAVAILABLE, REFUSAL_UNSATISFIABLE,
                     Clock, bounded, body_size, optional)
from .workload_authority import IntentLedger, LedgerFull

REANNOUNCE_PACE_S = 0.025  # <= 40 frames/s: the ingress budget is 50/s per connection
CONTRACT = "harmony.request/1"
LADDER = ("submitted", "evaluated", "placed", "done")
TERMINAL = ("done", "refused", "cancelled", "expired")
MAX_LIVE = 256
MAX_BYTES = 1024 * 1024

_INTENT_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
_INTERVAL = re.compile(r"^\s*([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])\s*$")
_CMPS = (">=", "<=", "!=", ">", "<")
_BODY_FIELDS = {"intent_id", "scope", "host", "require", "prefer"}
_SCALAR = (str, int, float, bool)


class Inexpressible(Exception):
    def __init__(self, clause: str, detail: str) -> None:
        super().__init__(f"{clause}: {detail}")
        self.clause, self.detail = clause, detail


def _number(raw: str) -> float:
    raw = raw.strip()
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError as error:
        raise ValueError(f"not a number: {raw!r}") from error


def _coerce(value: Any) -> Any:
    """`"true"`/`"27"` become bool/number, as the request grammar does for string scalars."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
        try:
            return _number(value)
        except ValueError:
            return value.strip()
    return value


def split_key(key: str) -> Tuple[str, str]:
    for cmp_ in _CMPS:
        if key.endswith(cmp_):
            return key[: -len(cmp_)].strip(), cmp_
    return key.strip(), ""


def expand(label: str, key: str, value: Any) -> List[Tuple[str, Any]]:
    """One clause -> flat planner clauses `[(key<op>, value)]`, or Inexpressible naming `label`."""
    name, op = split_key(key)
    if not name:
        raise Inexpressible(label, "empty attribute name")
    if name == "adapter" and not op:
        if not isinstance(value, str) or not value.strip():
            raise Inexpressible(label, "adapter= names no adapter")
        return [(f"adapter.{value.strip()}", True)]
    if isinstance(value, dict):
        if set(value) != {"not"}:
            raise Inexpressible(label, f"value shape not in the language: object with keys {sorted(value)}")
        inner = value["not"]
        if isinstance(inner, list):
            if not inner or not all(isinstance(x, _SCALAR) for x in inner):
                raise Inexpressible(label, "negation of an empty or non-scalar list")
            return [(f"{name}!=", [_coerce(x) for x in inner])]
        if not isinstance(inner, _SCALAR):
            raise Inexpressible(label, "negation of a non-scalar value")
        return [(f"{name}!=", _coerce(inner))]
    if isinstance(value, list):
        if not value or not all(isinstance(x, _SCALAR) for x in value):
            raise Inexpressible(label, "any-of list is empty or has a non-scalar member")
        return [(key.strip(), [_coerce(x) for x in value])]
    if isinstance(value, str):
        match = _INTERVAL.match(value)
        if match:
            lo_b, lo, hi, hi_b = match.groups()
            out: List[Tuple[str, Any]] = []
            try:
                if lo.strip():
                    out.append((name + (">=" if lo_b == "[" else ">"), _number(lo)))
                if hi.strip():
                    out.append((name + ("<=" if hi_b == "]" else "<"), _number(hi)))
            except ValueError as error:
                raise Inexpressible(label, f"interval {value!r}: {error}") from error
            if not out:
                raise Inexpressible(label, f"interval {value!r} constrains nothing")
            return out
        text = value.strip()
        if text and (text[0] in "[(" or text[-1] in "])"):
            raise Inexpressible(label, f"malformed interval {value!r}")
    if isinstance(value, _SCALAR):
        return [(key.strip(), _coerce(value))]
    raise Inexpressible(label, f"value shape not in the language: {type(value).__name__}")


def parse_clauses(section: str, clauses: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """`[(label, flat_key, flat_value)]` for a whole require/prefer map; `label` names the source clause."""
    out: List[Tuple[str, str, Any]] = []
    for key, value in clauses.items():
        label = key if section == "require" else f"prefer.{key}"
        for flat_key, flat_value in expand(label, str(key), value):
            out.append((label, flat_key, flat_value))
    return out


def vocabulary(units: Sequence[dict]) -> Dict[str, List[Any]]:
    """attribute name -> every value some unit in scope declares for it (the open vocabulary)."""
    vocab: Dict[str, List[Any]] = {}
    for unit in units:
        for name, value in (unit.get("attributes") or {}).items():
            vocab.setdefault(name, []).append(value)
    return vocab


def check_vocabulary(clauses: List[Tuple[str, str, Any]], vocab: Dict[str, List[Any]]) -> None:
    for label, flat_key, value in clauses:
        name, op = split_key(flat_key)
        if name not in vocab:
            raise Inexpressible(label, f"attribute {name!r} is declared by no unit in scope")
        if op in (">=", ">", "<=", "<") and not any(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in vocab[name]):
            raise Inexpressible(label, f"interval/comparison over {name!r}, whose declared values are not numbers")


def reference_satisfies(attributes: dict, requires: Dict[str, Any]) -> bool:
    """Stdlib double of `planner._unit_satisfies` (same semantics; pinned to it by a parity test)."""
    for key, want in requires.items():
        name, op = split_key(key)
        if name not in attributes:
            return False
        have = attributes[name]
        try:
            if op == "":
                if isinstance(want, (list, tuple, set)):
                    if have not in want:
                        return False
                elif have != want:
                    return False
            elif op == "!=":
                if isinstance(want, (list, tuple, set)):
                    if have in want:
                        return False
                elif have == want:
                    return False
            elif op == ">=" and not float(have) >= float(want):
                return False
            elif op == ">" and not float(have) > float(want):
                return False
            elif op == "<=" and not float(have) <= float(want):
                return False
            elif op == "<" and not float(have) < float(want):
                return False
        except (TypeError, ValueError):
            return False
    return True


def reference_candidates(units: Sequence[dict], requires: Dict[str, Any]) -> List[dict]:
    """Units meeting every clause, resident first then smaller footprint, then name (planner ordering)."""
    fits = [u for u in units if reference_satisfies(u.get("attributes") or {}, requires)]
    return sorted(fits, key=lambda u: (not u.get("resident"), u.get("footprint", 0), u.get("reload_cost", 1.0),
                                       u.get("name", "")))


def request_ledger(path: str, clock: Optional[Clock] = None) -> IntentLedger:
    """The ledger with this contract's catalog bounds (max_live 256, max_bytes 1 MiB)."""
    return IntentLedger(path, clock=clock, max_live=MAX_LIVE, max_bytes=MAX_BYTES)


Candidates = Callable[[Sequence[dict], Dict[str, Any]], List[dict]]
Publish = Callable[[dict], Awaitable[Any]]  # sends an `intent_partial` frame (service-minted epoch/seq)


class RequestAuthority:
    """Handles `write` frames for harmony.request/1."""

    def __init__(self, ledger: IntentLedger, host: str, scope_units: Callable[[str, Optional[str]], Sequence[dict]],
                 publish: Publish, source: str, candidates: Candidates = reference_candidates,
                 place: Optional[Callable[[dict], Optional[dict]]] = None,
                 lease_active: Optional[Callable[[str], bool]] = None, clock: Optional[Clock] = None,
                 log: Callable[[str], None] = lambda _m: None) -> None:
        self.ledger, self.host, self.scope_units = ledger, host, scope_units
        self.publish, self.source, self.candidates = publish, source, candidates
        self.place, self.lease_active = place, lease_active
        self.clock, self.log = optional(clock), log
        self.epoch = 0

    def _partial(self, intent_id: str, seq: int, body: dict) -> dict:
        if self.epoch <= 0 or seq <= 0:
            raise ValueError("request intent partial needs a positive authority epoch and sequence")
        return {"contract": CONTRACT, "source": self.source, "subject": {"intent_id": intent_id}, "epoch": self.epoch,
                "seq": seq, "produced_ms": self.clock(), "ttl_ms": 0, "status": "ok", "body": body}

    def _unrecorded_refusal(self, intent_id: str, code: str, detail: str, scope: str = "fleet") -> dict:
        return self._partial(intent_id, 1, {"intent_id": intent_id, "status": "refused", "scope": scope,
                                            "refusal": code, "detail": bounded(detail, 256)})

    async def handle_write(self, write: Any) -> dict:
        try:
            if write.op == "submit":
                return await self._submit(write)
            if write.op == "cancel":
                return await self._cancel(write)
            return self._unrecorded_refusal(write.intent_id, REFUSAL_STEP_NOT_ALLOWED, f"{write.op}_is_authority_only")
        except LedgerFull as error:
            self.log(f"request: refused {write.intent_id}: {error}")
            return self._unrecorded_refusal(write.intent_id, REFUSAL_UNAVAILABLE, f"intent_ledger_full:{error}")

    def _validate(self, write: Any) -> Tuple[Optional[dict], Optional[Tuple[str, str]]]:
        payload = write.payload or {}
        body = payload.get("body")
        if not isinstance(body, dict):
            return None, (REFUSAL_SCHEMA_INVALID, "payload.body must be an object")
        if set(payload) - {"body"}:
            return None, (REFUSAL_FORBIDDEN_FIELD, f"payload.{sorted(set(payload) - {'body'})[0]}")
        if body_size(body) > MAX_BODY_BYTES:
            return None, (REFUSAL_OVERSIZE, f"body exceeds {MAX_BODY_BYTES} bytes")
        if set(body) - _BODY_FIELDS:
            return None, (REFUSAL_FORBIDDEN_FIELD, f"body.{sorted(set(body) - _BODY_FIELDS)[0]}")
        if not _INTENT_ID.match(write.intent_id or "") or body.get("intent_id", write.intent_id) != write.intent_id:
            return None, (REFUSAL_SCHEMA_INVALID, "intent_id must match the frame and be 1..64 of [A-Za-z0-9_.:@-]")
        if not isinstance(getattr(write, "principal", None), str) or not 1 <= len(write.principal) <= 128:
            return None, (REFUSAL_SCHEMA_INVALID, "write.principal must name the authenticated requester")
        if body.get("scope") not in ("host", "fleet"):
            return None, (REFUSAL_SCHEMA_INVALID, "body.scope must be 'host' or 'fleet'")
        if "host" in body and body["scope"] != "host":
            return None, (REFUSAL_SCHEMA_INVALID, "body.host is only valid for host scope")
        if body.get("host") is not None and not (isinstance(body["host"], str) and len(body["host"]) <= 128):
            return None, (REFUSAL_SCHEMA_INVALID, "body.host must be a string of at most 128 characters")
        for section in ("require", "prefer"):
            value = body.get(section, {})
            if not isinstance(value, dict) or len(value) > 16:
                return None, (REFUSAL_SCHEMA_INVALID, f"body.{section} must be an object of at most 16 clauses")
        if "require" not in body:
            return None, (REFUSAL_SCHEMA_INVALID, "body.require is required")
        return body, None

    async def _submit(self, write: Any) -> dict:
        body, problem = self._validate(write)
        prior = self.ledger.get(write.intent_id)
        if prior is not None:
            if prior["owner"] != getattr(write, "principal", None):
                return self._unrecorded_refusal(write.intent_id, REFUSAL_STEP_NOT_ALLOWED,
                                                "intent_id belongs to another requester")
            if problem is None and prior["body"] == body:
                return prior["partial"]
            return self._unrecorded_refusal(write.intent_id, REFUSAL_SCHEMA_INVALID,
                                            "intent_id_reused_with_a_different_body", prior["kind"])
        if problem is not None:
            self.log(f"request: refused {write.intent_id}: {problem[0]} {problem[1]}")
            scope = (write.payload or {}).get("body", {}).get("scope") if isinstance((write.payload or {}).get("body"), dict) else None
            return self._unrecorded_refusal(write.intent_id, problem[0], problem[1], scope if scope in ("host", "fleet") else "fleet")
        scope = body["scope"]
        record = {"intent_id": write.intent_id, "kind": scope, "body": body, "status": "submitted", "terminal": False,
                  "seq": 1, "job": None, "created_ms": self.clock(), "owner": write.principal}
        record["partial"] = self._partial(write.intent_id, 1, {"intent_id": write.intent_id, "status": "submitted",
                                                              "scope": scope, **({"host": body["host"]} if body.get("host") else {})})
        self.ledger.put(record)
        await self._publish(record["partial"])
        return await self._evaluate(record)

    async def _evaluate(self, record: dict) -> dict:
        body = record["body"]
        scope, host = body["scope"], body.get("host")
        if scope == "host" and host not in (None, self.host):
            return await self._advance(record, "refused", {
                "refusal": REFUSAL_SCHEMA_INVALID, "detail": f"host_scope_mismatch: this broker serves {self.host!r}, not {host!r}"})
        units = list(self.scope_units(scope, host or (self.host if scope == "host" else None)))
        try:
            require = parse_clauses("require", body.get("require") or {})
            prefer = parse_clauses("prefer", body.get("prefer") or {})
            if not require:
                raise Inexpressible("require", "a requirement was given but constrains nothing")
            vocab = vocabulary(units)
            check_vocabulary(require + prefer, vocab)
        except Inexpressible as error:
            self.log(f"request: {record['intent_id']} requirement_inexpressible clause={error.clause}: {error.detail}")
            return await self._advance(record, "refused", {"refusal": REFUSAL_INEXPRESSIBLE, "clause": bounded(error.clause, 128),
                                                           "detail": bounded(error.detail, 256)})
        requires = dict(self._flatten(require))
        try:
            fits = list(self.candidates(units, requires))
        except Exception as error:  # noqa: BLE001 - the planner failing is not "unsatisfiable"
            self.log(f"request: planner failed for {record['intent_id']}: {error!r}")
            return await self._advance(record, "refused", {"refusal": REFUSAL_UNAVAILABLE,
                                                           "detail": bounded(f"planner_error:{error}", 256)})
        if not fits:
            return await self._advance(record, "refused", {
                "refusal": REFUSAL_UNSATISFIABLE,
                "detail": bounded("no unit meets " + ", ".join(f"{k}={v}" for k, v in requires.items()), 256)})
        if prefer:
            wants = dict(self._flatten(prefer))
            fits.sort(key=lambda u: -sum(1 for k, v in wants.items() if self.candidates([u], {k: v})))
        best = fits[0]
        fields = {"unit": bounded(best.get("name", ""), 128)}
        if best.get("host"):
            fields["host"] = bounded(best["host"], 128)
        record["job"] = json.dumps({"unit": best.get("name"), "host": best.get("host")})
        partial = await self._advance(record, "evaluated", fields)
        return await self._settle(record, best) or partial

    @staticmethod
    def _flatten(clauses: List[Tuple[str, str, Any]]) -> List[Tuple[str, Any]]:
        merged: Dict[str, Any] = {}
        for _, key, value in clauses:
            merged[key] = value
        return list(merged.items())

    async def _settle(self, record: dict, unit: dict) -> Optional[dict]:
        """evaluated -> placed -> done when the unit is resident; None while it is not."""
        if not unit.get("resident"):
            return None
        extra: Dict[str, Any] = {"unit": bounded(unit.get("name", ""), 128)}
        if unit.get("host"):
            extra["host"] = bounded(unit["host"], 128)
        grant = self.place(unit) if self.place else None
        if grant and grant.get("lease"):
            extra["lease"] = bounded(grant["lease"], 128)
        partial = await self._advance(record, "placed", extra)
        if "lease" not in extra:
            partial = await self._advance(record, "done", extra)
        return partial

    async def _cancel(self, write: Any) -> dict:
        record = self.ledger.get(write.intent_id)
        if record is None:
            return self._unrecorded_refusal(write.intent_id, REFUSAL_SCHEMA_INVALID, "unknown_intent")
        if record["owner"] != getattr(write, "principal", None):
            return self._unrecorded_refusal(write.intent_id, REFUSAL_STEP_NOT_ALLOWED,
                                            "only the authenticated requester may cancel this intent", record["kind"])
        if record["terminal"]:
            return record["partial"]
        if record["status"] not in ("submitted", "evaluated"):
            current = json.loads(json.dumps(record["partial"]))
            current["body"].update(refusal=REFUSAL_STEP_NOT_ALLOWED, detail="cancel_is_allowed_only_before_placed")
            return current
        return await self._advance(record, "cancelled", {})

    async def _advance(self, record: dict, status: str, extra: Dict[str, Any]) -> dict:
        record["seq"] += 1
        body = {"intent_id": record["intent_id"], "status": status, "scope": record["kind"]}
        if record["body"].get("host") and "host" not in extra:
            body["host"] = record["body"]["host"]
        body.update(extra)
        record["status"], record["terminal"] = status, status in TERMINAL
        record["partial"] = self._partial(record["intent_id"], record["seq"], body)
        self.ledger.put(record)
        await self._publish(record["partial"])
        return record["partial"]

    async def _publish(self, partial: dict) -> None:
        try:
            await self.publish(partial)
        except Exception as error:  # noqa: BLE001
            self.log(f"request: publish of {partial['subject']['intent_id']} failed: {error!r}")

    async def pump(self) -> int:
        """Advance requests waiting on residency or a lease; end the overdue ones."""
        if self.epoch == 0:
            return 0  # not the authority yet (no realm epoch): publishing now would be unversioned
        changed = 0
        for record in self.ledger.live():
            info = json.loads(record["job"]) if record["job"] else None
            if record["status"] == "evaluated" and info:
                units = [u for u in self.scope_units(record["kind"], record["body"].get("host") or (
                    self.host if record["kind"] == "host" else None)) if u.get("name") == info["unit"]
                         and (info.get("host") in (None, u.get("host")))]
                if units and await self._settle(record, units[0]):
                    changed += 1
            elif record["status"] == "placed" and self.lease_active:
                lease = record["partial"]["body"].get("lease")
                if lease and not self.lease_active(lease):
                    await self._advance(record, "done", {k: v for k, v in record["partial"]["body"].items()
                                                         if k in ("unit", "host", "lease")})
                    changed += 1
        for record in self.ledger.overdue():
            await self._advance(record, "expired", {"detail": "unresolved_for_30_days"})
            changed += 1
        self.ledger.prune()
        return changed

    async def reannounce(self, epoch: int) -> int:
        self.epoch = epoch
        count = 0
        for record in self.ledger.live():
            record["seq"] += 1
            record["partial"] = self._partial(record["intent_id"], record["seq"], record["partial"]["body"])
            self.ledger.put(record)
            await self._publish(record["partial"])
            count += 1
            await asyncio.sleep(REANNOUNCE_PACE_S)
        self.log(f"request: authority epoch {epoch}; re-announced {count} non-terminal intents")
        return count
