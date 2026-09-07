"""A local LLM as a Harmony unit.

The point of routing an LLM through Harmony rather than letting it hold VRAM
statically: on a box that also serves ASR and TTS, the LLM is the largest
resident and the most interruptible. Harmony can shed it when someone is
dictating and bring it back when they stop. A bare `vllm serve` cannot be shed —
it owns the card until someone kills it.

Shape: vLLM runs as a SUBPROCESS, and the ManagedUnit's loader/freer start and
stop it. That is what makes eviction real — terminating the process is the only
way to return its VRAM to the driver, because the memory belongs to that process
(same reason the broker has a `reclaim` lever it cannot exercise itself).

This process holds no GPU memory of its own; it is a facade plus a proxy.
"""
from __future__ import annotations

import os
import json
import shlex
import signal
import subprocess
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

HOST_ID = os.environ.get("HARMONY_LLM_HOST_ID", "xc-tower-ubuntu")
MODEL = os.environ.get("HARMONY_LLM_MODEL", "Qwen/Qwen3-8B")
NODE_PORT = int(os.environ.get("HARMONY_LLM_PORT", "8188"))
VLLM_PORT = int(os.environ.get("HARMONY_LLM_VLLM_PORT", "8189"))
GPU_FRACTION = os.environ.get("HARMONY_LLM_GPU_FRACTION", "0.60")
MAX_MODEL_LEN = os.environ.get("HARMONY_LLM_MAX_MODEL_LEN", "")
CUDA_DEVICE = os.environ.get("HARMONY_LLM_CUDA_DEVICE", "1")

# MORE THAN ONE MODEL, ONE PER UNIT.
#
# A node used to serve exactly one model, so "which model" and "which card" were
# the same question and both were answered in a systemd file. That is the
# decision Harmony exists to make: with two models declared as two units, the
# planner places them, and a workload that alternates between them settles them
# one per card because sharing would thrash (livestack planner `_contention_cost`).
#
#   HARMONY_LLM_UNITS='[{"name":"llm_title","model":"ibm-granite/granite-4.2-8b",
#                        "port":8189,"footprint_gb":18,"gpu_fraction":"0.85"},
#                       {"name":"llm_judge","model":"cyankiwi/gemma-4-26B-A4B-it-AWQ-4bit",
#                        "port":8191,"footprint_gb":15,"gpu_fraction":"0.62"}]'
#
# Unset => exactly one unit named "llm" from the single-model variables above,
# so an existing deployment is byte-for-byte unchanged.
#
# Prefer HARMONY_LLM_UNITS_FILE for a systemd deployment: systemd processes
# quotes in `Environment=`, which silently mangles inline JSON — the value
# arrives with its property-name quotes stripped and json.loads fails at
# `line 1 column 3`. A file has no such rules.
_UNITS_FILE = os.environ.get("HARMONY_LLM_UNITS_FILE", "").strip()
_UNITS_ENV = os.environ.get("HARMONY_LLM_UNITS", "").strip()
if _UNITS_FILE:
    with open(_UNITS_FILE, "r", encoding="utf-8") as _fh:
        _UNITS_ENV = _fh.read().strip()
IDLE_EVICT_SECONDS = int(os.environ.get("HARMONY_LLM_IDLE_EVICT_SECONDS", "900"))
# Extra words for the `vllm serve` argv, split with shlex so quoted values
# survive ("--max-num-seqs 32 --dtype bfloat16"). Empty by default; model swaps
# (e.g. INT4 candidates needing --quantization) ride this instead of a code edit.
EXTRA_ARGS = shlex.split(os.environ.get("HARMONY_LLM_EXTRA_ARGS", ""))
# Warm-on-start: after attach, pre-load the LLM so the first request does not
# eat the cold start. Any value but "0"/"false" means on; set "0" to debug
# cold-start behaviour without the preload racing your observation.
WARM_ON_START = os.environ.get("HARMONY_LLM_WARM_ON_START", "1").strip().lower() not in {"0", "false"}
# Declared footprint. Qwen3-8B bf16 weights are ~16 GB; the rest is KV cache
# governed by GPU_FRACTION. The planner reconciles this against the live CUDA
# meter and uses the TIGHTER of the two, so an under-declaration here cannot
# cause an OOM grant — it would only make the planner over-optimistic until the
# meter corrects it.
FOOTPRINT = int(float(os.environ.get("HARMONY_LLM_FOOTPRINT_GB", "17")) * (1 << 30))
# Units in one contention class are alternatives for the same sort of work. The
# planner charges for co-residence in proportion to the DEMAND waiting for each,
# so alternating traffic separates them and idle siblings cost nothing.
SPREAD_GROUP = os.environ.get("HARMONY_LLM_SPREAD_GROUP", "llm")


def _unit_specs() -> "list[dict]":
    if not _UNITS_ENV:
        return [{"name": "llm", "model": MODEL, "port": VLLM_PORT,
                 "footprint_gb": FOOTPRINT / (1 << 30), "gpu_fraction": GPU_FRACTION,
                 "max_model_len": MAX_MODEL_LEN, "extra_args": EXTRA_ARGS}]
    out = []
    for spec in json.loads(_UNITS_ENV):
        out.append({
            "name": spec["name"],
            "model": spec["model"],
            "port": int(spec.get("port", VLLM_PORT)),
            "footprint_gb": float(spec.get("footprint_gb", FOOTPRINT / (1 << 30))),
            "gpu_fraction": str(spec.get("gpu_fraction", GPU_FRACTION)),
            "max_model_len": str(spec.get("max_model_len", MAX_MODEL_LEN) or ""),
            "extra_args": shlex.split(spec.get("extra_args", "")) or EXTRA_ARGS,
        })
    if not out:
        raise RuntimeError("HARMONY_LLM_UNITS is set but declares no units")
    return out


SPECS = {u["name"]: u for u in _unit_specs()}
try:
    from livestack_node.facade import resolve_device_id as _rdi
    DEVICE_ID_SELF = _rdi(HOST_ID)
except Exception:
    DEVICE_ID_SELF = ""
VLLM_BASE = f"http://127.0.0.1:{VLLM_PORT}"   # single-unit compatibility alias

# One vLLM subprocess per unit, keyed by unit name. Eviction is process death —
# that is the only thing that returns VRAM to the driver — so a unit's process
# is its residency.
_procs: "dict[str, subprocess.Popen]" = {}
_lock = threading.RLock()


def _base_of(name: str) -> str:
    return f"http://127.0.0.1:{SPECS[name]['port']}"


def _vllm_up(timeout: float = 2.0, name: str = "") -> bool:
    name = name or next(iter(SPECS))
    try:
        r = httpx.get(f"{_base_of(name)}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _load(name: str = "", device: "str | None" = None):
    """Start this unit's vLLM and block until it actually serves.

    `device` is the placement the PLANNER chose, arriving through
    livestack's warm path. This node is pinned to one card for metering (a
    wrapper that saw every card would report card 0's pressure for a model on
    card 1 — the trap the comment below records), so the assignment is checked
    against the card this node speaks for rather than used to move the process.
    A node that is told to load somewhere it does not serve says so, instead of
    quietly loading in the wrong place and letting the planner believe its own
    plan.

    Returning before the server is up would let Harmony mark the unit resident
    and let a request through to a port that is not listening yet.
    """
    name = name or next(iter(SPECS))
    spec = SPECS[name]
    with _lock:
        p = _procs.get(name)
        if p is not None and p.poll() is None and _vllm_up(name=name):
            return p
        env = dict(os.environ)
        # Only set this if the unit did not already. Setting it ONLY here is a
        # trap: the child then runs on the right card while THIS process — which
        # is where livestack's CUDA meter runs, and therefore where the device
        # pressure the planner reads comes from — still sees every card and
        # meters device 0. Observed: the LLM on card 1 reporting card 0's
        # pressure, identical to the three ASR/TTS nodes actually on card 0.
        # The unit sets CUDA_VISIBLE_DEVICES process-wide so the wrapper, its
        # meter and the child all agree.
        if "CUDA_VISIBLE_DEVICES" not in env and CUDA_DEVICE:
            env["CUDA_VISIBLE_DEVICES"] = CUDA_DEVICE
        cmd = [
            os.path.join(os.path.dirname(__file__), "venv", "bin", "vllm"),
            "serve", spec["model"],
            "--port", str(spec["port"]),
            "--host", "127.0.0.1",
            "--gpu-memory-utilization", spec["gpu_fraction"],
            "--served-model-name", spec["model"], name, "local",
        ]
        if spec["max_model_len"]:
            cmd += ["--max-model-len", spec["max_model_len"]]
        cmd += spec["extra_args"]
        print(f"[harmony-llm] starting vLLM for {name}"
              f"{f' (planner chose {device})' if device else ''}: {' '.join(cmd)}", flush=True)
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
        _procs[name] = proc
        deadline = time.time() + float(os.environ.get("HARMONY_LLM_START_TIMEOUT", "900"))
        while time.time() < deadline:
            if proc.poll() is not None:
                # The return code alone is not a diagnosis. rc=2 here was
                # argparse rejecting `--disable-log-requests`, removed in vLLM
                # 0.28 — invisible until the journal was read by hand. Point at
                # the log that has the answer.
                _procs.pop(name, None)
                raise RuntimeError(
                    f"vLLM exited during startup of {name} (rc={proc.returncode}); "
                    f"see `journalctl -u harmony-llm` for its stderr")
            if _vllm_up(name=name):
                print(f"[harmony-llm] vLLM ready: {name}", flush=True)
                return proc
            time.sleep(2)
        _free(name)
        raise RuntimeError(f"vLLM for {name} did not become ready before the deadline")


def _free(name: str = ""):
    """Stop this unit's vLLM and WAIT for it to die. Returning while the process
    is still exiting would report VRAM freed that the driver has not reclaimed
    yet, and the planner would then grant against memory that is still held."""
    name = name or next(iter(SPECS))
    with _lock:
        p = _procs.pop(name, None)
        if p is None or p.poll() is not None:
            return
        print(f"[harmony-llm] stopping vLLM: {name}", flush=True)
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            p.terminate()
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print(f"[harmony-llm] vLLM ({name}) ignored SIGTERM; killing", flush=True)
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                p.kill()
            p.wait(timeout=30)


def _health_probe_for(name: str):
    """A per-unit functional probe bound to that unit's port."""
    def probe(_model) -> bool:
        try:
            r = httpx.post(
                f"{_base_of(name)}/v1/chat/completions",
                json={"model": SPECS[name]["model"], "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ok"}]},
                timeout=30,
            )
            return r.status_code == 200
        except Exception:
            return False
    return probe


def _health_probe(_model) -> bool:
    """FUNCTIONAL probe, not a liveness ping: the unit is healthy only if it
    can actually complete. A vLLM that is up and answering /health while its
    engine has died would otherwise keep attracting traffic."""
    try:
        r = httpx.post(
            f"{VLLM_BASE}/v1/chat/completions",
            json={"model": MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ok"}]},
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


app = FastAPI(title="harmony-llm", version="1.0.0")


def _gpu_call(fn):
    with _lock:
        return fn()


from livestack_node import ManagedUnit, ResidencyPolicy, attach, counting  # noqa: E402
from livestack_node.client import admit  # noqa: E402

_UNITS = {
    name: ManagedUnit(
        name,
        # `device` is accepted, so livestack passes the planner's placement in
        # (ManagedUnit introspects the loader). A loader without it is called
        # as before, which is why every other node in the fleet is unaffected.
        loader=(lambda n=name: (lambda device=None: _load(n, device)))(),
        freer=(lambda n=name: (lambda: _free(n)))(),
        footprint=int(spec["footprint_gb"] * (1 << 30)),
        # SOFT_PIN. Measured 2026-09-05: an evicted unit takes ~50.7 s to answer
        # its first request, against the hub's 35 s title timeout and 15 s
        # attention timeout — a cold start is a missed title every time. Not
        # HARD_PIN: HARD_PIN means "never preempted", and the point of routing
        # the LLM through Harmony is that it CAN shed the LLM under real
        # pressure — SOFT_PIN still evicts then, HARD_PIN refuses to.
        residency_policy=getattr(ResidencyPolicy,
                                 os.environ.get("HARMONY_LLM_RESIDENCY", "SOFT_PIN").upper()),
        health_check=_health_probe_for(name),
        spread_group=SPREAD_GROUP,
    )
    for name, spec in SPECS.items()
}


def _readiness() -> dict:
    live = [n for n in SPECS if _vllm_up(name=n)]
    return {
        "ready": bool(live),
        "detail": ("serving " + ", ".join(live)) if live else "no unit resident",
        "model": ", ".join(SPECS[n]["model"] for n in live) or MODEL,
    }


# This node PROXIES; it never takes a livestack lease per request, so the
# lease-derived in_flight the facade would otherwise infer reads 0 no matter how
# many generations are in flight. Count our own, and let the facade label it
# `in_flight_source: "server"` so a consumer can tell that 0 means idle.
_busy = counting()

manager, residence = attach(
    app, host_id=HOST_ID, kind="llm", units=_UNITS,
    idle_seconds=IDLE_EVICT_SECONDS, coload=False,
    gpu_call=_gpu_call, port=NODE_PORT, readiness=_readiness,
    in_flight=_busy,
)


if WARM_ON_START:
    def _warm_on_start():
        # Give the facade a moment to bind before the first ensure, so the
        # load does not race attach's own startup bookkeeping.
        time.sleep(2)
        try:
            first = next(iter(SPECS))
            manager.ensure(first)
            print(f"[harmony-llm] warm-on-start: {first} resident", flush=True)
        except Exception as e:
            print(f"[harmony-llm] warm-on-start failed: {e}", flush=True)

    threading.Thread(target=_warm_on_start, daemon=True).start()


BROKER_URLS = [u.strip() for u in
               os.environ.get("LIVESTACK_BROKER_URL", "").split(",") if u.strip()]
# A single-unit node on a single-card box has nothing to arbitrate; going
# through admission there would only add a round trip. Set this when a node
# shares its card with other tenants that Harmony may need to shed for it.
MULTI_NODE = os.environ.get("HARMONY_LLM_ADMIT", "").strip().lower() in {"1", "true", "yes"}
# Long: admission BLOCKS while the broker evicts victims and warms the grant,
# and warming a 15 GB model is minutes, not seconds.
ADMIT_TIMEOUT = float(os.environ.get("HARMONY_LLM_ADMIT_TIMEOUT", "600"))


def _peer_at(device_id: str) -> "str | None":
    """The base URL of the node that speaks for this device, when it is not us.

    From the broker's `/peers`, which is the only surface that carries a node's
    URL: `/status` reports device_id, memory and units but no address, so an
    earlier version of this that read `node_url`/`base` off `/status` could
    never match anything and silently never forwarded.

    Without forwarding, a node asked for a unit the planner placed elsewhere
    would load a second copy on its own card — deciding placement again, which
    is the thing admission exists to take away from it.
    """
    if not device_id:
        return None
    for base in BROKER_URLS:
        try:
            rows = httpx.get(f"{base}/peers", timeout=3.0).json()
        except Exception:
            continue
        rows = rows if isinstance(rows, list) else rows.get("peers", [])
        for r in rows:
            if r.get("device_id") != device_id:
                continue
            url = (r.get("peer") or "")
            if not url or r.get("device_id") == DEVICE_ID_SELF:
                continue
            return url.rsplit("/livestack", 1)[0]
        return None
    return None


def _unit_for_model(requested: str) -> str:
    """Which declared unit serves this `model` field.

    Accepts the unit name, the model id, or the legacy alias `local`. An
    unknown model resolves to the first declared unit rather than erroring,
    which keeps every existing caller — all of which send `local` — working.
    """
    r = (requested or "").strip()
    if r in SPECS:
        return r
    for name, spec in SPECS.items():
        if spec["model"] == r:
            return name
    return next(iter(SPECS))


@app.get("/health")
def health():
    return {"status": "ok",
            "units": {n: {"model": SPECS[n]["model"], "resident": _vllm_up(name=n)}
                      for n in SPECS},
            # Single-unit shape, kept so existing health checks still parse.
            "model": SPECS[next(iter(SPECS))]["model"],
            "resident": any(_vllm_up(name=n) for n in SPECS)}


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    """OpenAI-compatible surface. Every call goes through `manager.ensure`, so a
    request against an evicted unit reloads it through Harmony's admission
    (which makes room first) rather than racing the planner."""
    body = await request.body()
    # Route by the requested model, so one node can serve several. The unit is
    # ensured BEFORE the request goes anywhere: a call against an evicted unit
    # reloads it through Harmony's admission (which makes room first) rather
    # than racing the planner.
    unit = next(iter(SPECS))
    if body:
        try:
            unit = _unit_for_model(json.loads(body).get("model", ""))
        except Exception:
            pass
    # ADMISSION FIRST, then load. Loading straight off the request is how a node
    # ends up starting vLLM into whatever memory happens to be free, with 10 GB
    # of idle ASR and TTS on the card that nobody ever asked to move:
    #
    #   ValueError: Free memory on device cuda:0 (6.9/23.56 GiB) on startup is
    #   less than desired GPU memory utilization (0.62, 14.61 GiB)
    #
    # `admit` asks Harmony to make room and returns WHERE it granted. The broker
    # evicts victims and warms the grant before it answers, so by this point the
    # unit is resident on the granted device. Two consequences worth stating:
    # the placement is the planner's, not this node's, and a broker that does
    # not answer degrades to loading locally exactly as before.
    granted, degraded = None, None
    if len(SPECS) > 1 or MULTI_NODE:
        try:
            res = admit(unit, owner_id=f"harmony-llm:{HOST_ID}", timeout=ADMIT_TIMEOUT)
            granted, degraded = res.get("device_id"), res.get("degraded")
        except Exception as e:                # arbitration must not take us offline
            degraded = f"{type(e).__name__}: {e}"
    if degraded:
        print(f"[harmony-llm] admission degraded for {unit}: {degraded}", flush=True)

    elsewhere = None
    if granted and DEVICE_ID_SELF and granted != DEVICE_ID_SELF:
        elsewhere = _peer_at(granted)
        if elsewhere is None:
            raise HTTPException(
                status_code=503,
                detail=f"{unit} was placed on {granted}, which is not this node "
                       f"and has no reachable peer")

    if elsewhere:
        url = f"{elsewhere}/v1/{path}"
    else:
        try:
            manager.ensure(unit)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"{unit} unavailable: {e}")
        url = f"{_base_of(unit)}/v1/{path}"

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length"}}
    timeout = httpx.Timeout(float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")))
    client = httpx.AsyncClient(timeout=timeout)
    try:
        req = client.build_request(request.method, url, content=body, headers=headers,
                                   params=dict(request.query_params))
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"vllm proxy failed: {e}")

    async def body_iter():
        # The request is in flight until the LAST byte has been streamed to the
        # caller, not until the upstream accepted it — a generation that is still
        # producing tokens is still occupying the card.
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            _busy.release()

    _busy.acquire()
    return StreamingResponse(
        body_iter(), status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in {"content-length", "transfer-encoding"}},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=NODE_PORT)
