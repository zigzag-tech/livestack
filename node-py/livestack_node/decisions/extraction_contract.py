"""Structured extraction is not a classifier score or an input-submission grant."""
from collections.abc import Mapping

from .contract import ContractError

PROFILE_ID = "pane-ask-27b-v1:unqualified"
KINDS = {"confirm", "choice", "multi", "input", "open"}


def validate_extraction(value):
    def invalid(detail):
        raise ContractError("invalid_output", "extraction: " + detail, 422)

    if not isinstance(value, Mapping):
        invalid("value is not an object")
    if value.get("outcome") == "no_grounded_ask":
        if set(value) != {"outcome"}:
            invalid("no_grounded_ask carries extra fields")
        return
    allowed = {"outcome", "request_quote", "kind", "question", "cue", "options"}
    if value.get("outcome") != "ask" or set(value) - allowed:
        invalid("unknown outcome or fields")
    if not isinstance(value.get("kind"), str) or value["kind"] not in KINDS:
        invalid("unknown ask kind")
    for field, maximum in (("request_quote", 300), ("question", 160), ("cue", 24)):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > maximum:
            invalid("invalid " + field)
        if field != "cue" and not text.strip():
            invalid("empty " + field)
    options = value.get("options", [])
    # Extract offered options; the host retains only four. Never grow with input.
    if not isinstance(options, list) or len(options) > 64:
        invalid("options exceed their bound")
    for option in options:
        if not isinstance(option, Mapping) or set(option) - {"label", "reply", "detail"}:
            invalid("invalid option")
        for field, maximum in (("label", 40), ("reply", 80), ("detail", 120)):
            text = option.get(field, "")
            if not isinstance(text, str) or len(text) > maximum:
                invalid("invalid option " + field)
