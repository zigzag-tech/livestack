"""The request language, parsed -- with no units in sight.

`model: "require:class=llm,params_b=[20,30)"` and `harmony_requires` are turned
into the flat `key<op>: value` clauses the planner and the fleet ranker compare,
and a request's path/body imply clauses of their own (an image implies vision).
None of that depends on which units a node declares, so a node with no units at
all -- the localhost router (router.py) -- parses a request exactly as the
facade (server.py) does. One parser: a clause cannot mean one thing at the
router and another at the target.
"""
from __future__ import annotations

import re

from fastapi import HTTPException


#
# A closed grammar over an OPEN vocabulary: the shapes below are the whole
# language, while the attribute names are whatever the units declare. That is
# what buys expressiveness without turning this into a query engine.
#
# The broker's matcher takes flat `key<op>: value` clauses ANDed together, so
# everything richer is normalized into that shape HERE. The caller gets the
# expressive surface; the planner keeps the interface it already has.
_INTERVAL_RE = re.compile(r"^\s*([\[\(])\s*([^,]*)\s*,\s*([^\]\)]*)\s*([\]\)])\s*$")


def split_clauses(text: str) -> "list[str]":
    """Split `require:` clauses on commas that are NOT inside an interval.

    `params_b=[20,30)` carries a comma that belongs to the interval, not to the
    clause list. Splitting naively produced `params_b=[20` and `30)` — two
    unparseable clauses from one valid requirement.
    """
    out, depth, cur = [], 0, []
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [c for c in (x.strip() for x in out) if c]


def num(raw: str):
    raw = raw.strip()
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        raise ValueError(f"not a number: {raw!r}")


def expand_clause(key: str, val) -> "list[tuple[str, object]]":
    """One requirement entry -> the flat clauses the broker understands.

    Interval notation is used for ranges because open vs closed bounds are the
    entire point and every operator-in-the-key spelling gets one of them wrong:
        "[20,30)"  ->  params_b>=20 AND params_b<30
        "[20,]"    ->  params_b>=20
        "(,30]"    ->  params_b<=30
    """
    # `adapter=chips-v1` asks for a unit serving that LoRA. Units carry one
    # boolean attribute per adapter, so this is an ordinary equality clause the
    # broker's planner compares without knowing what an adapter is.
    if key == "adapter":
        name = str(val).strip()
        if not name:
            raise ValueError("adapter= names no adapter")
        return [(f"adapter.{name}", True)]
    if isinstance(val, str):
        m = _INTERVAL_RE.match(val)
        if m:
            lo_b, lo, hi, hi_b = m.groups()
            out = []
            if lo.strip():
                out.append((key + (">=" if lo_b == "[" else ">"), num(lo)))
            if hi.strip():
                out.append((key + ("<=" if hi_b == "]" else "<"), num(hi)))
            if not out:
                raise ValueError(f"interval {val!r} constrains nothing")
            return out
    return [(key, val)]




def requirement_from(body_json: dict, is_adapter=lambda model: False) -> "dict | None":
    """A stated NEED instead of a model name, in either of two shapes.

    Body field (what an OpenAI SDK sends via extra_body):
        {"harmony_requires": {"class": "llm", "params_b>": 7, "params_b<=": 10}}
    Model string (for clients that can only set `model`):
        {"model": "require:class=llm,params_b>7,params_b<=10"}

    The point of both is that a caller says what it needs and never learns which
    model is loaded, on which card, or what had to move — that is the planner's
    business, and asking a caller to know it is what makes a GPU fleet feel like
    a set of machines instead of one.
    """
    raw_req = body_json.get("harmony_requires")
    entries: "list[tuple[str, object]]" = []
    if isinstance(raw_req, dict) and raw_req:
        entries = list(raw_req.items())
    else:
        model = str(body_json.get("model") or "")
        if not model.startswith("require:"):
            # An ADAPTER's name as the model is how vLLM selects a LoRA, and so
            # it is what a node that resolved `adapter=<name>` forwards to the
            # peer holding the unit. Read it back as that requirement: treated
            # as an unknown name it resolved "any llm" and the normalisation
            # below rewrote `model` to the base -- base weights and a 200.
            if is_adapter(model):
                return {f"adapter.{model}": True}
            return None
        for clause in split_clauses(model[len("require:"):]):
            clause = clause.strip()
            if not clause:
                continue
            # `=` LAST: `params_b>=20` must not partition on the `=`.
            for op in (">=", "<=", "!=", ">", "<", "="):
                if op in clause:
                    name, _, rest = clause.partition(op)
                    rest = rest.strip()
                    if op == "=":
                        entries.append((name.strip(), rest))
                    else:
                        entries.append((name.strip() + op, rest))
                    break
            else:
                # FAIL CLOSED. The old code dropped an unparseable clause and
                # carried on, so `params_b~20` quietly became "no size
                # requirement" and the caller got whatever was resident.
                raise HTTPException(
                    status_code=400,
                    detail=f"harmony: cannot parse requirement clause {clause!r}")

    out: dict = {}
    for key, val in entries:
        try:
            for k, v in expand_clause(str(key), val):
                # Numeric strings become numbers, and "true"/"false" become
                # bools: leaving them as strings is how `thinking=true` failed
                # to match a unit whose attribute is a real boolean.
                if isinstance(v, str):
                    low = v.strip().lower()
                    if low in ("true", "false"):
                        v = low == "true"
                    else:
                        try:
                            v = num(v)
                        except ValueError:
                            v = v.strip()
                out[k] = v
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"harmony: bad requirement {key!r}: {e}")
    if not out:
        raise HTTPException(
            status_code=400,
            detail="harmony: a requirement was given but constrains nothing")
    return out


def derived_requirements(path: str, body_json: dict) -> dict:
    """What the request already tells us. Never ask a caller to declare this.

    A request carrying an image has said it needs vision; a request that turns
    thinking on has said it needs a unit that can SEPARATE that thinking from
    the answer. Making the caller restate either is the bookkeeping this system
    exists to abolish — and in the thinking case, not deriving it is what let a
    request enable thinking on a unit with no reasoning parser and put the
    model's narration into a user-visible reply (2026-09-07).
    """
    out: dict = {}
    # No leading slash in either clause: `path` is the {path:path} capture and
    # arrives WITHOUT the "/v1/" prefix, as "chat/completions" / "completions" /
    # "embeddings". Spelled "/completions", the second clause matched
    # "chat/completions" by luck and a bare "completions" not at all — which
    # derived NO class for /v1/completions and, now that a node can declare a
    # pooling unit, would let a generation request be routed to one.
    if "completions" in path:
        out["class"] = "llm"
    # The endpoint says what KIND of work this is, exactly as it does for chat.
    # No leading slash: `path` is the {path:path} capture and arrives WITHOUT
    # the "/v1/" prefix, as "embeddings". (The chat clause above matches only
    # because "chat/completions" happens to contain "/completions".)
    # Without this clause an embedding request derives nothing, falls past
    # candidate_kinds() into _unit_for_model()'s positional fallback, and is
    # served by whichever unit happens to be declared first — a generation unit,
    # which answers /v1/embeddings with a 400. Deriving it is what lets a node
    # declare both kinds and route each correctly.
    elif "embeddings" in path:
        out["class"] = "embed"

    # Vision: an image part anywhere in the messages. Deliberately NOT a
    # recursive scan for `type: image_url` — arbitrary user JSON containing that
    # shape is not an image, and treating it as one would route text work to a
    # vision unit for no reason.
    for msg in (body_json.get("messages") or []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                    out["vision"] = True
                    break
        if out.get("vision"):
            break

    # Thinking: the PARAMETER implies the CAPABILITY. These are two different
    # things (see docs/livestack-harmony.md) and conflating them is the bug.
    kwargs = body_json.get("chat_template_kwargs")
    if isinstance(kwargs, dict) and kwargs.get("enable_thinking") is True:
        out["thinking"] = True

    # Tools: a request that ships tool schemas has said it needs a unit that can
    # CALL them, exactly as an image says it needs vision. Nobody should have to
    # add `tools=true` to a requirement string — the tools are right there in the
    # body. `tool_choice: "none"` is the one case that ships schemas without
    # needing the capability, so it does not derive.
    tools = body_json.get("tools")
    if isinstance(tools, list) and tools and body_json.get("tool_choice") != "none":
        out["tools"] = True
    return out
