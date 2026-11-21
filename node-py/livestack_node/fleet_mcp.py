"""Adding a model to Harmony, served by the fleet broker (MCP + plain HTTP).

An agent anywhere on the fleet that wants to add a model (or anything else
Harmony should run) needs three things it cannot see from its own machine:

* the RUNBOOK, i.e. which of the routes applies and where the config lives;
* the VOCABULARY, i.e. which attribute names and values units declare today, so a
  new unit is findable by the requests callers already write and does not
  invent `param_b` beside `params_b`;
* a FIT and MATCH check against the live fleet view: does the proposed unit fit
  the card beside its current tenants, and which units would a stated need
  select before and after the new unit exists.

The broker is the one service that holds the whole-fleet view and that every
machine already reaches, so it serves all three. That makes the update path
central: the runbook ships in this package (runbooks/*.md) and is served from
whatever revision the broker runs, so one broker redeploy updates every agent.

Everything here is a READ over `broker.fleet_view()`: nothing is reserved,
loaded, evicted or written. The change itself still lands as reviewed host
config (see the runbook); a "deploy" tool would bring back the hand edits the
drift alarm exists to catch.

MCP transport: the streamable-HTTP shape with plain JSON responses (one
JSON-RPC object in, one out; notifications get 202). No SSE stream and no
session state are needed, because every tool is a stateless read.
"""
import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .requirements import attrs_meet, split_key

RUNBOOK_DIR = Path(__file__).parent / "runbooks"
GIB = 1024 ** 3
# Kept free on a card when judging "fits": CUDA context, allocator slack, and the
# activation peak a declared footprint underestimates. A guess, stated as one.
DEVICE_SLACK_BYTES = 1 * GIB
# The router (localhost ingress) forwards only these paths; anything else needs
# its own ingress, or the router taught a new path.
ROUTED_CLASSES = {"llm": "chat/completions, completions", "embed": "embeddings"}
# The planner never offers a unit declaring one of these to demand that does not
# name it (planner._SPECIALIST_ONLY_ATTRS). Repeated here because a proposal is
# not a planner Unit; keep the two in step.
SPECIALIST_ONLY_ATTRS = ("ocr", "depth_guide", "inpaint")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


# --------------------------------------------------------------------------- the fleet, flattened

def _units(view: Mapping) -> List[dict]:
    """Every declared unit on a node the broker can currently read."""
    out = []
    for host, row in (view.get("hosts") or {}).items():
        for node in row.get("nodes") or []:
            if node.get("state") not in ("fresh", "suspect"):
                continue
            mem = node.get("device_mem") or (node.get("load") or {}).get("device") or {}
            for u in node.get("units") or []:
                out.append({
                    "host": host, "node": node.get("peer"), "device_id": node.get("device_id"),
                    "region": node.get("region"), "kind": u.get("kind"),
                    "attributes": dict(u.get("attributes") or {}),
                    "vram_bytes": int((u.get("footprint") or {}).get("vram_bytes") or 0),
                    "ram_bytes": int((u.get("footprint") or {}).get("ram_bytes") or 0),
                    "resident": bool(u.get("resident")), "busy": bool(u.get("busy")),
                    "residency": u.get("residency"), "priority": u.get("priority"),
                    "device_capacity": mem.get("capacity"), "device_free": mem.get("free"),
                })
    return out


def _host_of(device_id: Optional[str]) -> Optional[str]:
    return device_id.split("/", 1)[0] if device_id else None


def vocabulary(view: Mapping) -> dict:
    """The attribute names and values units declare today, and where."""
    attrs: Dict[str, dict] = {}
    kinds: Dict[str, List[str]] = {}
    devices: Dict[str, dict] = {}
    for u in _units(view):
        where = f"{u['host']}/{u['kind']}"
        kinds.setdefault(u["kind"], []).append(u["host"])
        for name, val in u["attributes"].items():
            a = attrs.setdefault(name, {"values": set(), "units": []})
            for v in (val if isinstance(val, (list, tuple)) else [val]):
                a["values"].add(json.dumps(v))
            a["units"].append(where)
        if u["device_id"]:
            d = devices.setdefault(u["device_id"], {
                "capacity_bytes": u["device_capacity"], "free_bytes": u["device_free"], "units": []})
            d["units"].append({"kind": u["kind"], "vram_bytes": u["vram_bytes"],
                               "resident": u["resident"], "residency": u["residency"]})
    classes = sorted({str(u["attributes"]["class"]) for u in _units(view) if "class" in u["attributes"]})
    return {
        "classes": classes,
        "attributes": {k: {"values": [json.loads(x) for x in sorted(v["values"])][:24],
                           "units": sorted(set(v["units"]))}
                       for k, v in sorted(attrs.items())},
        "kinds": {k: sorted(set(h)) for k, h in sorted(kinds.items())},
        "devices": devices,
        "specialist_only_attributes": list(SPECIALIST_ONLY_ATTRS),
        "routed_by_router": ROUTED_CLASSES,
        "sources": view.get("sources", {"self": "live"}),
        "note": ("Only nodes this broker can read right now are listed. A unit with no `class` attribute "
                 "(older polytts/polyasr nodes) is reachable only by naming its node's own API."),
    }


# --------------------------------------------------------------------------- needs

def _load_request_language():
    """The ONE `require:` parser lives with the harmony-llm facade; reuse it, never fork it."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "examples" / "harmony-llm" / "request_language.py"
    spec = importlib.util.spec_from_file_location("_harmony_request_language", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_need(need: Any) -> dict:
    """`"require:class=llm,params_b=[20,]"` or a `harmony_requires` dict -> flat clauses."""
    if isinstance(need, Mapping) and need:
        body = {"harmony_requires": dict(need)}
    elif isinstance(need, str) and need.strip():
        s = need.strip()
        body = {"model": s if s.startswith("require:") else "require:" + s}
    else:
        raise ValueError("need is empty: give `require:class=...` or a harmony_requires object")
    rl = _load_request_language()
    try:
        return rl.requirement_from(body) or {}
    except Exception as exc:  # HTTPException from the shared parser -> a plain refusal here
        raise ValueError(getattr(exc, "detail", None) or str(exc)) from exc


def _gaps(attributes: Mapping, requires: Mapping) -> List[str]:
    return [f"{k}={json.dumps(w)}" for k, w in requires.items() if not attrs_meet(attributes, {k: w})]


def _specialist_blocked(attributes: Mapping, requires: Mapping) -> List[str]:
    held = [a for a in SPECIALIST_ONLY_ATTRS if attributes.get(a) in (True, "true", "1", "yes")]
    return held if held and not any(split_key(k)[0] in held for k in requires) else []


def match(view: Mapping, need: Any, *, extra_unit: Optional[dict] = None) -> dict:
    """Which declared units satisfy a need, and why the others do not.

    The candidate SET, not the ranking: which one actually serves also depends on
    residency, load and distance (`POST /harmony/route` on a router answers that).
    """
    requires = parse_need(need)
    units = _units(view)
    if extra_unit:
        units = units + [extra_unit]
    known = {n for u in units for n in u["attributes"]}
    unknown = sorted({split_key(k)[0] for k in requires} - known)
    hits, misses = [], []
    for u in units:
        gaps = _gaps(u["attributes"], requires)
        blocked = _specialist_blocked(u["attributes"], requires)
        row = {"unit": f"{u['host']}/{u['kind']}", "device_id": u["device_id"], "resident": u["resident"],
               "proposed": bool(u.get("proposed"))}
        if not gaps and not blocked:
            hits.append(row)
        else:
            misses.append({**row, "why": "; ".join(
                ([f"unmet {', '.join(gaps)}"] if gaps else [])
                + ([f"specialist-only ({', '.join(blocked)}) and the need names none"] if blocked else []))})
    out = {"requires": requires, "matches": hits, "non_matches": misses[:20]}
    if unknown:
        out["inexpressible"] = {
            "attributes": unknown,
            "why": "no unit declares these names, so the planner would refuse with requirement_inexpressible",
            "close_known": {n: difflib.get_close_matches(n, sorted(known), n=3) for n in unknown}}
    return out


# --------------------------------------------------------------------------- a proposed unit

def plan_unit(view: Mapping, proposal: Mapping) -> dict:
    """Judge a proposed unit against the live fleet before anyone writes config.

    proposal: {kind, host | device_id, attributes{class,...}, footprint{vram_bytes, ram_bytes},
               exclusive_device?, residency?, caller_need?}
    Returns blocking `errors`, advisory `warnings`, per-device `fit`, and what callers would match.
    """
    errors: List[str] = []
    warnings: List[str] = []
    kind = str(proposal.get("kind") or "").strip()
    device_id = proposal.get("device_id")
    host = proposal.get("host") or _host_of(device_id)
    attrs = dict(proposal.get("attributes") or {})
    fp = dict(proposal.get("footprint") or {})
    vram = int(float(fp.get("vram_bytes") or 0) or float(fp.get("vram_gb") or 0) * GIB)
    ram = int(float(fp.get("ram_bytes") or 0) or float(fp.get("ram_gb") or 0) * GIB)
    cpu_only = str(attrs.get("backend", "")).lower() == "cpu"
    if not kind:
        errors.append("`kind` is required: the unit's name on its node (e.g. `upscale_seedvr2`)")
    if not host:
        errors.append("`host` (or `device_id`) is required: which machine will serve it")
    if not vram and not cpu_only:
        errors.append("`footprint.vram_bytes` (or vram_gb) is required: weights + PEAK activation, measured, "
                      "not the checkpoint size")

    units = _units(view)
    vocab = vocabulary(view)
    known = set(vocab["attributes"])
    if "class" not in attrs:
        errors.append(f"`attributes.class` is required; classes today: {', '.join(vocab['classes']) or 'none'}")
    else:
        cls = str(attrs["class"])
        if cls not in vocab["classes"]:
            warnings.append(f"new class `{cls}`: callers will write require:class={cls}. "
                            + ("The localhost router does not forward it; give it an ingress or add a router path."
                               if cls not in ROUTED_CLASSES else ""))
    for name in sorted(set(attrs) - known - {"class"}):
        close = difflib.get_close_matches(name, sorted(known), n=3)
        warnings.append(f"attribute `{name}` is new to the fleet"
                        + (f" (did you mean {', '.join('`%s`' % c for c in close)}?)" if close else
                           " (fine if intended: it becomes vocabulary callers can require)"))
    for u in units:
        if u["host"] == host and u["kind"] == kind:
            errors.append(f"kind `{kind}` is already declared on {host} ({u['node']}): rename it, or edit that unit")

    host_units = [u for u in units if u["host"] == host or _host_of(u["device_id"]) == host]
    if host and not host_units:
        warnings.append(f"the broker sees no live node on `{host}`: no fit data. A new host is fine; "
                        "its first node announces itself on start (see the runbook).")
    fit = []
    for dev in sorted({u["device_id"] for u in host_units if u["device_id"]}):
        if device_id and dev != device_id:
            continue
        tenants = [u for u in host_units if u["device_id"] == dev]
        cap = next((u["device_capacity"] for u in tenants if u["device_capacity"]), None)
        free = next((u["device_free"] for u in tenants if u["device_free"] is not None), None)
        if cap is None:
            fit.append({"device_id": dev, "verdict": "unknown", "why": "node reports no device memory"})
            continue
        resident = [u for u in tenants if u["resident"]]
        pinned = [u for u in tenants if u["residency"] == 0]
        usable = cap - DEVICE_SLACK_BYTES
        if vram > usable:
            verdict, why = "never_fits", f"needs {vram / GIB:.1f} GiB, card has {cap / GIB:.1f} GiB"
        elif free is not None and vram <= free - DEVICE_SLACK_BYTES:
            verdict, why = "fits_now", f"{(free - vram) / GIB:.1f} GiB left beside today's residents"
        elif vram <= usable - sum(u["vram_bytes"] for u in pinned):
            verdict = "fits_after_eviction"
            why = ("the planner must evict " + ", ".join(f"{u['kind']} ({u['vram_bytes'] / GIB:.1f} GiB)"
                                                         for u in resident) + " to load it")
            for u in resident:
                elsewhere = [o for o in units if o["kind"] == u["kind"] and o["resident"]
                             and o["device_id"] != dev]
                if not elsewhere:
                    warnings.append(f"loading on {dev} evicts the fleet's ONLY resident `{u['kind']}`: its "
                                    "callers cold-load or wait while yours runs. Prefer another card, or agree "
                                    "it with the unit's owner.")
        else:
            verdict, why = "blocked_by_pins", "HARD_PIN units on this card leave too little room"
        row = {"device_id": dev, "capacity_gib": round(cap / GIB, 1),
               "free_gib": None if free is None else round(free / GIB, 1),
               "tenants": [{"kind": u["kind"], "vram_gib": round(u["vram_bytes"] / GIB, 1),
                            "resident": u["resident"], "residency": u["residency"]} for u in tenants],
               "verdict": verdict, "why": why}
        if proposal.get("exclusive_device") and tenants:
            row["exclusive_note"] = ("exclusive_device holds the WHOLE card while resident: "
                                     + ", ".join(u["kind"] for u in tenants)
                                     + " cannot load until it leaves (the 2026-11-04 FLUX-vs-speech lesson)")
        fit.append(row)
    if fit and all(r["verdict"] in ("never_fits", "blocked_by_pins") for r in fit):
        errors.append("it fits no card on this host: pick another host or a smaller quantization")
    if ram:
        warnings.append(f"declares ram_bytes {ram / GIB:.1f} GiB of host RAM: check the host's free RAM "
                        "(the broker plans it against the host pool)")

    proposed = {"host": host, "node": "(proposed)", "device_id": device_id, "kind": kind, "attributes": attrs,
                "vram_bytes": vram, "resident": False, "proposed": True}
    callers = {}
    need = proposal.get("caller_need")
    if need:
        try:
            m = match(view, need, extra_unit=proposed)
            callers = {"need": need, "selects_new_unit": any(h["proposed"] for h in m["matches"]),
                       "also_matches": [h["unit"] for h in m["matches"] if not h["proposed"]]}
            if not callers["selects_new_unit"]:
                errors.append(f"caller_need `{need}` does not select the new unit: "
                              + next((x["why"] for x in m["non_matches"] if x["proposed"]), "?"))
        except ValueError as exc:
            errors.append(f"caller_need does not parse: {exc}")
    # Always: does GENERIC demand for this class (what existing callers already send) now reach it?
    if "class" in attrs:
        generic = f"require:class={attrs['class']}"
        m = match(view, generic, extra_unit=proposed)
        others = [h["unit"] for h in m["matches"] if not h["proposed"]]
        reached = any(h["proposed"] for h in m["matches"])
        callers["generic"] = {"need": generic, "reaches_new_unit": reached, "existing_matches": others}
        if reached and others:
            warnings.append(f"generic demand `{generic}` (what callers of {', '.join(others[:3])} send) will now "
                            f"also be able to land on `{kind}`. If it should not answer that demand, declare a "
                            "specialist-only attribute (planner._SPECIALIST_ONLY_ATTRS) and have callers require it.")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "fit": fit, "callers": callers,
            "next": "Write the unit into the host's tracked config, deploy at idle, then verify with "
                    "`match_need` and one real request (runbook steps 5-7)."}


# --------------------------------------------------------------------------- runbooks

def runbooks() -> Dict[str, Path]:
    return {p.stem: p for p in sorted(RUNBOOK_DIR.glob("*.md"))}


def runbook_text(name: str) -> str:
    p = runbooks().get(name)
    if p is None:
        raise KeyError(name)
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- MCP

TOOLS = [
    {"name": "harmony_runbook",
     "description": ("Read a Harmony runbook. START HERE before adding a model or anything else for the fleet "
                     "to run: `add-model` says which route applies (LLM unit, attach() server, image worker, "
                     "CPU workload handler), where the config lives, and how to verify."),
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "default": "add-model"}}}},
    {"name": "harmony_vocabulary",
     "description": ("The attribute names/values units declare today, the classes, the kinds per host, and "
                     "each GPU's tenants and free memory. Use it to name a new unit's attributes so "
                     "callers' existing requests find it."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "harmony_plan_unit",
     "description": ("Check a PROPOSED unit against the live fleet before writing config: required fields, "
                     "kind collisions, new or misspelt attributes, whether it fits each card beside its "
                     "tenants (and what would be evicted), and which caller needs would select it. Read-only."),
     "inputSchema": {"type": "object", "required": ["kind", "attributes"], "properties": {
         "kind": {"type": "string", "description": "unit name on its node, e.g. upscale_seedvr2"},
         "host": {"type": "string", "description": "serving machine, e.g. zz-whitby"},
         "device_id": {"type": "string", "description": "optional: one card, e.g. zz-whitby/298bce32"},
         "attributes": {"type": "object", "description": "must include class; e.g. {class: imagegen, task: upscale}"},
         "footprint": {"type": "object", "description": "{vram_gb | vram_bytes, ram_gb | ram_bytes}"},
         "exclusive_device": {"type": "boolean"},
         "caller_need": {"type": "string", "description": "the require: string callers will send"}}}},
    {"name": "harmony_match_need",
     "description": ("Which declared units a caller's need selects (`require:class=llm,params_b=[20,]` or a "
                     "harmony_requires object), why the others do not, and whether any clause is "
                     "inexpressible. Use after deploying to prove the new unit is reachable."),
     "inputSchema": {"type": "object", "required": ["need"], "properties": {
         "need": {"description": "require: string or harmony_requires object"}}}},
]


def _text(obj: Any) -> dict:
    body = obj if isinstance(obj, str) else json.dumps(obj, indent=1, default=str)
    return {"content": [{"type": "text", "text": body}]}


def call_tool(view_fn: Callable[[], Mapping], name: str, args: Mapping) -> dict:
    try:
        if name == "harmony_runbook":
            return _text(runbook_text(str(args.get("name") or "add-model")))
        if name == "harmony_vocabulary":
            return _text(vocabulary(view_fn()))
        if name == "harmony_plan_unit":
            return _text(plan_unit(view_fn(), args))
        if name == "harmony_match_need":
            return _text(match(view_fn(), args.get("need")))
    except KeyError as exc:
        return {**_text(f"unknown runbook {exc}; have: {', '.join(runbooks())}"), "isError": True}
    except ValueError as exc:
        return {**_text(str(exc)), "isError": True}
    return {**_text(f"unknown tool {name!r}"), "isError": True}


def handle_rpc(view_fn: Callable[[], Mapping], msg: Mapping, *, revision: str = "unknown") -> Optional[dict]:
    """One JSON-RPC message -> its response (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                   "serverInfo": {"name": "harmony", "version": revision},
                   "instructions": ("Harmony fleet broker. To add a model or anything the fleet should run: "
                                    "read harmony_runbook first, then harmony_vocabulary and harmony_plan_unit "
                                    "before writing config, and harmony_match_need to verify.")})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        return ok(call_tool(view_fn, params.get("name"), params.get("arguments") or {}))
    if method == "resources/list":
        return ok({"resources": [{"uri": f"harmony://runbooks/{n}", "name": f"runbook: {n}",
                                  "mimeType": "text/markdown"} for n in runbooks()]})
    if method == "resources/read":
        name = str(params.get("uri", "")).rsplit("/", 1)[-1]
        try:
            return ok({"contents": [{"uri": params.get("uri"), "mimeType": "text/markdown",
                                     "text": runbook_text(name)}]})
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"no runbook {name!r}"}}
    if method == "prompts/list":
        return ok({"prompts": [{"name": "add-model", "description": "Add a model or workload to Harmony",
                                "arguments": [{"name": "what", "description": "the model or job to add"}]}]})
    if method == "prompts/get":
        what = (params.get("arguments") or {}).get("what") or "(describe the model or job)"
        return ok({"messages": [{"role": "user", "content": {"type": "text", "text":
                   f"Add this to Harmony: {what}\n\nFollow the runbook below exactly.\n\n"
                   + runbook_text("add-model")}}]})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def revision() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "--short=8", "HEAD"],
                              capture_output=True, text=True, timeout=3).stdout.strip() or "unknown"
    except Exception:
        return os.environ.get("HARMONY_ROUTER_REV", "unknown")[:8]


class MergedView:
    """This broker's view plus other brokers' `/fleet`, merged by host.

    A regional broker seeds only what its routers need (the cn one: LLM and embed
    nodes), while the image, OCR and speech nodes announce to the na broker.
    Adding a model needs the WHOLE fleet, so the views named in
    HARMONY_MCP_VIEWS (comma-separated `/fleet` URLs) are fetched in the
    background every `ttl_s` and merged in. A node already in this broker's view
    wins. A view that cannot be fetched is reported under `sources`, never
    silently dropped.
    """

    def __init__(self, own: Callable[[], Mapping], urls, ttl_s: float = 60.0, timeout_s: float = 45.0):
        import threading
        self.own, self.urls, self.ttl_s, self.timeout_s = own, list(urls), ttl_s, timeout_s
        self.remote: Dict[str, dict] = {}
        self.status: Dict[str, str] = {u: "not fetched yet" for u in self.urls}
        if self.urls:
            threading.Thread(target=self._loop, name="fleet-mcp-views", daemon=True).start()

    def _fetch(self, url: str) -> None:
        import time
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as r:
                self.remote[url] = json.loads(r.read())
            self.status[url] = f"ok at {time.strftime('%H:%M:%S')}"
        except Exception as exc:  # report, keep the last good copy
            self.status[url] = f"fetch failed: {exc}"

    def _loop(self) -> None:
        import time
        while True:
            for u in self.urls:
                self._fetch(u)
            time.sleep(self.ttl_s)

    def __call__(self) -> dict:
        view = dict(self.own())
        hosts = {h: {**row, "nodes": list(row.get("nodes") or [])} for h, row in (view.get("hosts") or {}).items()}
        seen = {n.get("peer") for row in hosts.values() for n in row["nodes"]}
        for url, rv in self.remote.items():
            for h, row in (rv.get("hosts") or {}).items():
                for n in row.get("nodes") or []:
                    if n.get("peer") not in seen:
                        hosts.setdefault(h, {"nodes": []})["nodes"].append(n)
                        seen.add(n.get("peer"))
        view["hosts"] = hosts
        view["sources"] = {"self": "live", **self.status}
        return view


def mount(app, view_fn: Callable[[], Mapping]) -> None:
    """POST /mcp (MCP, streamable HTTP, JSON responses) and the same reads as plain HTTP."""
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, Response
    rev = revision()
    urls = [u.strip() for u in os.environ.get("HARMONY_MCP_VIEWS", "").split(",") if u.strip()]
    view_fn = MergedView(view_fn, urls) if urls else view_fn

    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}}, status_code=400)
        if isinstance(msg, list):  # batches (2025-03-26) — answer each
            out = [r for r in (handle_rpc(view_fn, m, revision=rev) for m in msg) if r]
            return JSONResponse(out) if out else Response(status_code=202)
        r = handle_rpc(view_fn, msg, revision=rev)
        return JSONResponse(r) if r else Response(status_code=202)

    @app.get("/mcp")
    def mcp_stream():
        # No server-initiated messages: the spec's answer is 405 on GET.
        return Response(status_code=405)

    @app.get("/fleet/units/vocabulary")
    def http_vocabulary():
        return vocabulary(view_fn())

    @app.post("/fleet/units/plan")
    def http_plan(payload: dict = Body(...)):
        return plan_unit(view_fn(), payload)

    @app.post("/fleet/units/match")
    def http_match(payload: dict = Body(...)):
        try:
            return match(view_fn(), payload.get("need"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/runbooks/{name}", response_class=PlainTextResponse)
    def http_runbook(name: str):
        try:
            return runbook_text(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no runbook {name!r}; have: {', '.join(runbooks())}")
