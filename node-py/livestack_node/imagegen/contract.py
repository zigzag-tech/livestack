"""Small, bounded image request contract shared by ingress and workers."""
from __future__ import annotations

from livestack_node.planner import Unit, _unit_satisfies


def validate_request(body: dict) -> dict:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("prompt must be a nonempty string of at most 12000 characters")
    out = dict(body)
    for key, default in (("width", 1024), ("height", 1024)):
        value = body.get(key, default)
        if type(value) is not int or not 256 <= value <= 1024 or value % 16:
            raise ValueError(f"{key} must be a multiple of 16 between 256 and 1024")
        out[key] = value
    seed = body.get("seed", 42)
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed must be an integer between 0 and 2**63-1")
    out["seed"] = seed
    req = body.get("harmony_requires", {})
    if not isinstance(req, dict) or req.get("class", "imagegen") != "imagegen":
        raise ValueError("harmony_requires must describe class=imagegen")
    out["harmony_requires"] = {"class": "imagegen", "task": "text_to_image", **req}
    return out


def satisfies(attributes: dict, requirements: dict) -> bool:
    return _unit_satisfies(Unit(kind="candidate", footprint={}, attributes=attributes), requirements)


def resolve_worker(view: dict, grant: dict, requirements: dict) -> dict:
    """Resolve the planner's grant, verifying its advertised requirements again."""
    if not grant.get("granted") or not grant.get("device_id") or not grant.get("kind"):
        raise RuntimeError(grant.get("reason") or "Harmony did not grant a concrete image unit")
    matches = []
    for host, data in view.get("hosts", {}).items():
        for node in data.get("nodes", []):
            if node.get("device_id") != grant["device_id"] or node.get("state") != "fresh":
                continue
            for unit in node.get("units", []):
                if unit["kind"] == grant["kind"] and satisfies(unit.get("attributes", {}), requirements):
                    matches.append({"host": host, "peer": node["peer"], "unit": unit})
    if len(matches) != 1:
        raise RuntimeError(f"grant resolved to {len(matches)} fresh workers; refusing ambiguous routing")
    return matches[0]
