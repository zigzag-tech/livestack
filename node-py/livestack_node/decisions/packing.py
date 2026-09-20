"""Canonical decision packing (decision-pack-v1).

Laya format: [CLS] <type> question: <instructions> [SEP] [MASK] opt0 ... [SEP] state [SEP].

The host preserves the complete current assistant message and preceding user
message first and drops whole older turns. Truncating the current span is
insufficient context, not a suffix classification.

A fixture tokenizer produces stable token ids for contract tests. Real CUDA/MLX
adapters substitute the pinned model tokenizer but keep this layout and marker
algorithm.
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .identity import PACKING_VERSION, digest

CLS_ID = 101
SEP_ID = 102
MASK_ID = 103
PAD_ID = 0
TEXT_BUDGET_BYTES = 24 * 1024
ENVELOPE_BUDGET_BYTES = 64 * 1024
MAX_TURNS = 6
OPT_TOKEN_CAP = 48

TOKEN_RE = re.compile(r"\S+")


def fixture_token_id(token: str) -> int:
    return 1000 + (zlib.crc32(token.encode("utf-8")) & 0xFFFFFFFF) % 30000


def fixture_tokenize(text: str) -> List[int]:
    return [fixture_token_id(t) for t in TOKEN_RE.findall(text)]


def render_criterion(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def render_options(question: Mapping[str, Any], option_order: Optional[Sequence[str]] = None) -> List[str]:
    qtype = question["type"]
    crit = question.get("criteria") or {}
    if qtype == "choice":
        keys = list(option_order) if option_order is not None else list(crit.keys())
        out = []
        for key in keys:
            value = crit.get(key, "")
            out.append(key if value in (None, "") else "%s: %s" % (key, render_criterion(value)))
        return out
    if qtype == "noul":
        false_crit = crit.get("false")
        true_crit = crit.get("true")
        return [
            "false: " + (render_criterion(false_crit) if false_crit not in (None, "") else "no, the statement does not hold"),
            "true: " + (render_criterion(true_crit) if true_crit not in (None, "") else "yes, the statement holds"),
        ]
    raise ValueError("unsupported question type %s" % qtype)


def serialize_state_text(state: Mapping[str, Any]) -> str:
    parts = [
        "CURRENT:\n" + (state.get("current_agent_message") or ""),
        "USER:\n" + (state.get("preceding_user_message") or ""),
    ]
    for turn in state.get("recent_turns") or []:
        parts.append("%s:\n%s" % (turn["role"].upper(), turn.get("text") or ""))
    parts.append("EXECUTION:\n" + (state.get("execution") or "unknown"))
    return "\n\n".join(parts)


def admit_source(state: Mapping[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Drop whole older turns to fit the 24 KiB host text budget.

    If the two required messages cannot fit, coverage is insufficient.
    """
    current = state.get("current_agent_message") or ""
    user = state.get("preceding_user_message") or ""
    required = {"current_agent_message": current, "preceding_user_message": user,
                "recent_turns": [], "execution": state.get("execution") or "unknown",
                "source_coverage": "complete"}
    required_text = serialize_state_text(required)
    if len(required_text.encode("utf-8")) > TEXT_BUDGET_BYTES:
        required["source_coverage"] = "insufficient"
        return required, "insufficient"
    turns = list(state.get("recent_turns") or [])[:MAX_TURNS]
    kept: List[Mapping[str, Any]] = []
    omitted = 0
    for turn in reversed(turns):
        trial = dict(required)
        trial["recent_turns"] = [turn, *kept]
        if len(serialize_state_text(trial).encode("utf-8")) > TEXT_BUDGET_BYTES:
            omitted += 1
            continue
        kept = [turn, *kept]
    required["recent_turns"] = kept
    required["source_coverage"] = "omitted_older" if omitted or len(turns) < len(state.get("recent_turns") or []) else "complete"
    if omitted:
        required["source_coverage"] = "omitted_older"
    return required, required["source_coverage"]


def build_sequence(
    state_text: str,
    question: Mapping[str, Any],
    *,
    max_len: int,
    head_max_len: int,
    option_order: Optional[Sequence[str]] = None,
    tokenize=fixture_tokenize,
) -> Tuple[List[int], List[int]]:
    opts = render_options(question, option_order)
    ins = str(question["instructions"]).replace("[MASK]", " ")
    head_ids = tokenize("%s question: %s" % (question["type"], ins))
    opt_ids: List[List[int]] = []
    for opt in opts:
        opt_ids.append([MASK_ID] + tokenize(" " + opt.replace("[MASK]", " "))[:OPT_TOKEN_CAP])
    opt_budget = head_max_len - sum(len(o) for o in opt_ids)
    if opt_budget < 16:
        per = max(4, (head_max_len - 16) // max(1, len(opt_ids)))
        opt_ids = [o[:per] for o in opt_ids]
        opt_budget = head_max_len - sum(len(o) for o in opt_ids)
    head_ids = head_ids[: max(8, opt_budget)]
    ids = [CLS_ID] + head_ids + [SEP_ID]
    markers: List[int] = []
    for o in opt_ids:
        markers.append(len(ids))
        ids.extend(o)
    ids.append(SEP_ID)
    room = max(0, max_len - len(ids) - 1)
    st = tokenize(state_text.replace("[MASK]", " "))
    st = st[:room]
    ids = ids + st + [SEP_ID]
    return ids[:max_len], [m for m in markers if m < max_len]


def packed_state_hash(token_ids: Sequence[int]) -> str:
    blob = json.dumps(list(token_ids), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pack_request(
    state: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    tokenize=fixture_tokenize,
) -> Dict[str, Any]:
    admitted, coverage = admit_source(state)
    if coverage == "insufficient":
        return {
            "ok": False,
            "cause": "insufficient_context",
            "coverage": coverage,
            "packing_version": PACKING_VERSION,
        }
    state_text = serialize_state_text(admitted)
    max_len = int(profile["max_len"])
    head_max_len = int(profile["head_max_len"])
    rows = []
    state_hashes = []
    for q in questions:
        option_order = None
        if q["type"] == "choice":
            option_order = list(profile.get("option_order") or (q.get("criteria") or {}).keys())
        ids, markers = build_sequence(
            state_text, q, max_len=max_len, head_max_len=head_max_len,
            option_order=option_order, tokenize=tokenize,
        )
        if q["type"] == "choice" and (len(ids) < 4 or not markers):
            return {"ok": False, "cause": "insufficient_context", "coverage": "insufficient",
                    "packing_version": PACKING_VERSION}
        current_ids = tokenize(admitted["current_agent_message"])
        user_ids = tokenize(admitted["preceding_user_message"])
        # Decisive span must appear complete in the packed state region.
        if current_ids and not _contains_subseq(ids, current_ids):
            return {"ok": False, "cause": "insufficient_context", "coverage": "insufficient",
                    "packing_version": PACKING_VERSION}
        if user_ids and not _contains_subseq(ids, user_ids):
            return {"ok": False, "cause": "insufficient_context", "coverage": "insufficient",
                    "packing_version": PACKING_VERSION}
        rows.append({
            "id": q["id"],
            "token_ids": ids,
            "markers": markers,
            "option_order": option_order or ["false", "true"],
        })
        state_hashes.append(packed_state_hash(ids))
    # Chip rescue reuses the first row's state packing. For mixed questions the
    # state text is identical; hash the admitted state tokens alone.
    state_only = tokenize(state_text.replace("[MASK]", " "))
    return {
        "ok": True,
        "cause": None,
        "coverage": admitted["source_coverage"],
        "packing_version": PACKING_VERSION,
        "state_text": state_text,
        "packed_state_hash": packed_state_hash(state_only),
        "rows": rows,
        "admitted_state": admitted,
    }


def _contains_subseq(hay: Sequence[int], needle: Sequence[int]) -> bool:
    if not needle:
        return True
    n = len(needle)
    for i in range(0, len(hay) - n + 1):
        if list(hay[i:i + n]) == list(needle):
            return True
    return False


def noul_question(candidate_id: str, candidate_text: str, template: str) -> Dict[str, Any]:
    return {
        "id": candidate_id,
        "type": "noul",
        "instructions": template.replace("{candidate}", candidate_text),
        "criteria": {},
    }
