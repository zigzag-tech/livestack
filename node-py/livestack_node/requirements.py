"""The one requirement evaluator.

A request's `requires` map is evaluated by the planner (`_unit_satisfies`), the
facade (`_local_satisfies`), the fleet embed picker (`fleet_embed.attrs_meet`) and
the fleet ranker (`fleet_rank`). They all call `attrs_meet` here, so a clause
cannot mean one thing for a local unit and another for a fleet node.

A key may carry a comparison suffix (`params_b>=`); a bare key is equality. A LIST
want is "a value in this list". A want of `{"not": v}` / `{"not": [a, b]}` is
"declared, and not that value / any of those". An attribute the unit does not
declare is NOT satisfied -- silence is not a yes, or an unlabelled unit would
answer every question, and that holds for negation too.
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple

CMPS = (">=", "<=", "!=", ">", "<")


def split_key(key: str) -> Tuple[str, str]:
    """`context_len>=` -> ("context_len", ">="); a bare key has op ""."""
    for c in CMPS:
        if key.endswith(c):
            return key[: -len(c)].strip(), c
    return key.strip(), ""


def _is_many(want: Any) -> bool:
    return isinstance(want, (list, tuple, set))


def clause_meets(attributes: Mapping, key: str, want: Any) -> bool:
    """Does one unit attribute satisfy one `require` clause?"""
    name, op = split_key(key)
    if name not in attributes:
        return False
    have = attributes[name]
    try:
        # A unit attribute that lists values offers each of them (`task: [text_to_image, inpaint]`): a clause asking for
        # any one of them is met, and a negative clause only when the unit offers none of what it excludes.
        if _is_many(have):
            wanted = (want["not"] if isinstance(want, Mapping) and set(want) == {"not"} else want)
            hit = any(w in have for w in wanted) if _is_many(wanted) else wanted in have
            if isinstance(want, Mapping) and set(want) == {"not"}:
                return not hit
            if op in ("", "!="):
                return hit if op == "" else not hit
            return False                          # an ordering clause on a list of values is never met
        if isinstance(want, Mapping) and set(want) == {"not"}:
            neg = want["not"]
            return have not in neg if _is_many(neg) else bool(have != neg)
        if op == "":
            return have in want if _is_many(want) else bool(have == want)
        if op == "!=":
            return have not in want if _is_many(want) else bool(have != want)
        if op == ">=":
            return float(have) >= float(want)
        if op == ">":
            return float(have) > float(want)
        if op == "<=":
            return float(have) <= float(want)
        return float(have) < float(want)          # "<"
    except (TypeError, ValueError):
        return False                              # non-numeric where a number was needed


def attrs_meet(attributes: Mapping, requires: Mapping) -> bool:
    """Every clause of `requires` is met by `attributes`."""
    return all(clause_meets(attributes, k, w) for k, w in requires.items())
