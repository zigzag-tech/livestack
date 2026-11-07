"""Image ingress resolving semantic requirements through the fleet planner."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import urllib.error
from contextlib import ExitStack, contextmanager
from pathlib import Path

from fastapi import FastAPI, Body, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from livestack_node import fleet_auth, transport
from livestack_node.client import exclusive_lease

from .contract import resolve_worker, validate_request


_GENERATION_SETTINGS = (
    "steps", "true_cfg_scale", "guidance_scale", "negative_prompt", "text_encoder",
    "max_sequence_length", "quantization_backend",
)


def json_call(base: str, method: str, path: str, body=None, token=None, timeout=1200, lease_id=None,
              cancel=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if lease_id:
        headers["X-Harmony-Device-Lease"] = lease_id
    try:
        status, _, raw = transport.dial(base, method, path, headers=headers,
                                      body=json.dumps(body).encode() if body is not None else None, timeout=timeout,
                                      cancel=cancel)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Harmony HTTP {e.code}: {e.read().decode()[:500]}") from e
    if status >= 400:
        raise RuntimeError(f"Harmony HTTP {status}: {raw.decode()[:500]}")
    return json.loads(raw)


def _verify_guided_result(request, actual, target):
    requirements = request["harmony_requires"]
    if (actual.get("model") != requirements["model"]
            or actual.get("revision") != requirements["revision"]):
        raise RuntimeError("worker result does not match the requested base model/revision")
    if (actual.get("width") != request["width"]
            or actual.get("height") != request["height"]):
        raise RuntimeError("worker result dimensions do not match the requested output")

    evidence = actual.get("depth_guide")
    if not isinstance(evidence, dict):
        raise RuntimeError("worker result is missing depth guidance evidence")
    attributes = target["unit"].get("attributes", {})
    if ("controlnet_quantization" not in attributes
            or evidence.get("controlnet_quantization") != attributes["controlnet_quantization"]):
        raise RuntimeError("worker depth guidance evidence mismatch: controlnet_quantization")
    if "offload" in attributes and actual.get("offload") != attributes["offload"]:
        raise RuntimeError("worker offload evidence mismatch")
    expected = {
        "image_sha256": request["control_image"]["sha256"],
        "conditioning_scale": request["controlnet_conditioning_scale"],
        "controlnet_model": requirements["controlnet_model"],
        "controlnet_revision": requirements["controlnet_revision"],
        "controlnet_config_sha256": requirements["controlnet_config_sha256"],
        "control_guidance_start": 0.0,
        "control_guidance_end": 1.0,
    }
    for key, want in expected.items():
        got = evidence.get(key)
        if got != want or (key == "conditioning_scale" and isinstance(got, bool)):
            raise RuntimeError(f"worker depth guidance evidence mismatch: {key}")

    settings = actual.get("generation_settings")
    if not isinstance(settings, dict):
        raise RuntimeError("worker result is missing generation settings")
    for key in _GENERATION_SETTINGS:
        if key in attributes and settings.get(key) != attributes[key]:
            raise RuntimeError(f"worker generation setting mismatch: {key}")


def _exclusive_units(config):
    """Per-unit whole-device lease profiles: {unit kind: {broker, owner, device_ids,
    min_free_bytes, wait_timeout_s, ttl_seconds}}. Units not listed run without leases."""
    units = config.get("exclusive_units", {})
    if not isinstance(units, dict):
        raise ValueError("exclusive_units must map unit kinds to lease profiles")
    for kind, profile in units.items():
        if not isinstance(profile, dict):
            raise ValueError(f"exclusive_units.{kind} must be a configuration object")
        for key in ("broker", "owner"):
            if not isinstance(profile.get(key), str) or not profile[key]:
                raise ValueError(f"exclusive_units.{kind}.{key} must be a nonempty string")
        device_ids = profile.get("device_ids")
        if (not isinstance(device_ids, list) or not device_ids
                or any(not isinstance(value, str) or not value for value in device_ids)
                or len(set(device_ids)) != len(device_ids)):
            raise ValueError(f"exclusive_units.{kind}.device_ids must be a nonempty unique list")
        minimum = profile.get("min_free_bytes")
        if isinstance(minimum, dict):
            if (set(minimum) != set(device_ids)
                    or any(type(value) is not int or value <= 0 for value in minimum.values())):
                raise ValueError(f"exclusive_units.{kind}.min_free_bytes must positively cover every device_id")
        elif type(minimum) is not int or minimum <= 0:
            raise ValueError(f"exclusive_units.{kind}.min_free_bytes must be positive per device")
        for key in ("wait_timeout_s", "ttl_seconds"):
            value = profile.get(key, 240 if key == "wait_timeout_s" else 120)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"exclusive_units.{kind}.{key} must be positive")
    return units


@contextmanager
def _device_leases(profile, token, wait_deadline, cancel=None):
    """Whole-device leases for ``profile``, in the profile's order. While
    another request holds a device, this one queues on the host broker until
    ``wait_deadline`` (the caller's ``harmony_wait_s``, monotonic) rather than
    being refused, keeping the devices it already holds.

    Holding while waiting is safe only because every request takes its devices
    in ONE global order (sorted device id), whatever order its profile lists
    them in: two requests can then never each hold a device the other waits
    for. The profile order still decides the evidence (its first device is the
    one the unit is placed on)."""
    if profile is None:
        yield []
        return
    with ExitStack() as stack:
        minimum = profile["min_free_bytes"]
        grants = {}
        for device_id in sorted(profile["device_ids"]):
            grants[device_id] = stack.enter_context(exclusive_lease(
                "imagegen", owner_id=profile["owner"], token=token, brokers=[profile["broker"]],
                device_id=device_id,
                min_free_bytes=minimum[device_id] if isinstance(minimum, dict) else minimum,
                wait_timeout_s=profile.get("wait_timeout_s", 240),
                ttl_seconds=profile.get("ttl_seconds", 120),
                request_id=f"image-gateway-{secrets.token_hex(16)}",
                queue_wait_s=max(0.0, wait_deadline - time.monotonic()),
                cancel=cancel))
        yield [grants[device_id] for device_id in profile["device_ids"]]


WORKER_IDLE_POLL_S = 2.0


def _await_worker_idle(call, endpoint, deadline, poll_s=None, unreachable_s=30.0):
    """Wait until the worker's facade reports every unit idle, or ``deadline`` (monotonic).

    A worker call that was cut (the caller hung up) or failed does not stop the
    GPU work behind it at once. Releasing the device leases first left a FLUX job
    generating on both cards with no lease (xc-tower-ubuntu, 2026-11-07: 06:43 and
    08:47); the next request's drain saw its memory as external and timed out at
    240 s, three times in a row. So the leases are kept until the worker is idle.
    A facade that stops answering for ``unreachable_s`` is taken as gone (its
    process owns the worker), not as busy for the rest of the deadline."""
    unreachable_since = None
    while True:
        try:
            report = call(endpoint, "GET", "/livestack/residence", timeout=10)
            unreachable_since = None
            if not any(unit.get("busy") for unit in report.get("units", [])):
                return True
        except (RuntimeError, OSError, ValueError):
            unreachable_since = unreachable_since or time.monotonic()
            if time.monotonic() - unreachable_since >= unreachable_s:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(WORKER_IDLE_POLL_S if poll_s is None else poll_s)


def create_app(config=None, call=json_call):
    config = config or json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    worker_token = Path(config["worker_token_file"]).read_text().strip()
    fleet_token = Path(config["fleet_token_file"]).read_text().strip()
    # Callers authenticate as fleet principals (fleet_auth: `Authorization` names the principal, `X-Harmony-Owner`
    # the owner a delegating one acts for), from a 0600 file in LIVESTACK_FLEET_TOKENS shape. The worker token is the
    # gateway's credential for its own call to the worker; callers that still present it are admitted as before,
    # under the configured owner, and logged by fingerprint so they can be moved to their own principal, after which
    # `worker_token_callers: false` refuses it from callers.
    principals = fleet_auth.load_principals(Path(config["principals_file"]).read_text(), log=print) if config.get("principals_file") else {}
    worker_token_callers = config.get("worker_token_callers", True)
    request_timeout = config.get("request_timeout_s", 1200)
    if isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float)) or request_timeout <= 0:
        raise ValueError("request_timeout_s must be positive")
    exclusive_units = _exclusive_units(config)
    app = FastAPI(title="Harmony requirement-routed image generation")

    @app.get("/health")
    def health():
        return {"ok": True, "fleet": config["fleet"]}

    @app.post("/v1/images/generations")
    async def generate(http_request: Request, body: dict = Body(...),
                       authorization: str | None = Header(None),
                       x_harmony_owner: str | None = Header(None)):
        # A caller that hangs up must not keep GPUs: its leases are released and
        # its work is not started (or is cut, which the worker's facade sees).
        # Observed 2026-11-03 on xc-tower-ubuntu: histo's chain was killed while
        # its request queued; the gateway kept it, took both GPUs at 12:22 and
        # ran a ~35 min FLUX job for nobody while the chain's retry waited.
        cancel = transport.Cancel()

        async def watch():
            while not await http_request.is_disconnected():
                await asyncio.sleep(1.0)
            cancel.cancel()

        watcher = asyncio.ensure_future(watch())
        try:
            return await run_in_threadpool(serve, body, authorization, x_harmony_owner, cancel)
        finally:
            watcher.cancel()

    def caller(authorization, requested_owner):
        """The owner this request is admitted and accounted under, decided by the credential."""
        if worker_token_callers and secrets.compare_digest(authorization or "", f"Bearer {worker_token}"):
            print(f"image ingress: caller on the worker token (owner {config.get('owner', 'acct_harmony-image')}); give it a principal", flush=True)
            return config.get("owner", "acct_harmony-image"), None
        if not principals:
            raise HTTPException(401, "image ingress credential required")
        try:
            owner, principal = fleet_auth.authenticate(principals, authorization, requested_owner)
        except fleet_auth.AuthError as e:
            raise HTTPException(e.status, e.detail) from e
        return owner, principal.name

    def serve(body, authorization, requested_owner, cancel):
        owner, principal = caller(authorization, requested_owner)
        try:
            request = validate_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        try:
            # The observe-only fleet planner picks the unit and device; the worker's
            # local broker is the only authority allowed to load or evict there.
            # A request may say it can wait (`harmony_wait_s`): Harmony then holds it in its own queue until
            # the planner can place it (a unit still registering, a device being cleared), and answers with the
            # planner's last reason if the window runs out first. The call outlives the window, so the HTTP
            # timeout is the window plus a margin.
            wait_s = request["harmony_wait_s"]
            wait_deadline = time.monotonic() + wait_s
            # The fleet broker admits under the gateway's own fleet principal, which may act only for its own owners
            # (the hub's prefix): a caller's owner reaches the broker only where that principal relays callers
            # (`admit_as_caller`, a fleet principal with delegate_prefix "*"). Otherwise the gateway's configured
            # owner is admitted, and the caller's owner and principal are kept on the result.
            admission = {
                "requires": request["harmony_requires"],
                "wait_s": wait_s,
                "owner": owner if config.get("admit_as_caller") else config.get("owner", "acct_harmony-image"),
            }
            grant = call(config["fleet"], "POST", "/admit", admission, token=fleet_token,
                         timeout=wait_s + 60, cancel=cancel)
            view = call(config["fleet"], "GET", "/fleet", cancel=cancel)
            target = resolve_worker(view, grant, request["harmony_requires"])
            endpoint = target["peer"].removesuffix("/livestack")
            profile = exclusive_units.get(grant["kind"])
            # A unit declared exclusive runs only while its host broker holds
            # whole-device leases for it: Harmony drains the other tenants itself
            # (never an external stop), and other admissions wait for the release.
            with _device_leases(profile, fleet_token, wait_deadline, cancel) as device_grants:
                lease_ids = [lease["lease_id"] for lease in device_grants]
                lease_evidence = json.dumps(lease_ids, separators=(",", ":")) if lease_ids else None
                if cancel.cancelled:
                    raise transport.Cancelled("the caller hung up before its work started")
                worker_deadline = time.monotonic() + request_timeout
                try:
                    result = call(endpoint, "POST", "/v1/images/generations", request,
                                  token=worker_token, timeout=request_timeout,
                                  lease_id=lease_evidence, cancel=cancel) if lease_evidence else call(
                                      endpoint, "POST", "/v1/images/generations", request,
                                      token=worker_token, timeout=request_timeout, cancel=cancel)
                except BaseException:
                    if lease_ids:
                        print(f"image ingress: worker call to {endpoint} ended early; holding "
                              f"{len(lease_ids)} device lease(s) until it reports idle", flush=True)
                        idle = _await_worker_idle(call, endpoint, worker_deadline)
                        print(f"image ingress: {endpoint} {'idle' if idle else 'still busy at request_timeout_s'}; "
                              "releasing device leases", flush=True)
                    raise
                actual = result.get("harmony", {})
                if actual.get("unit") != grant["kind"] or actual.get("device_id") != grant["device_id"]:
                    raise RuntimeError("worker result does not match the fleet grant")
                if lease_ids and actual.get("device_leases") != [
                        {"lease_id": value, "device_id": device}
                        for value, device in zip(lease_ids, profile["device_ids"])]:
                    raise RuntimeError("worker result does not match every device lease")
                if request["harmony_requires"].get("depth_guide") is True:
                    _verify_guided_result(request, actual, target)
                actual["fleet_grant"] = grant
                actual["owner"] = owner
                if principal:
                    actual["principal"] = principal
                if device_grants:
                    actual["device_grants"] = device_grants
                actual["worker_endpoint"] = endpoint
                return result
        except (RuntimeError, OSError, ValueError) as e:
            # A socket timeout has no message: name the exception, so a 503 always says what failed (an empty detail hid
            # a stalled worker call from histo's scroll on 2026-11-07).
            raise HTTPException(503, str(e) or f"{type(e).__name__} during the image request") from e
    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    uvicorn.run(create_app(config), host=os.environ.get("LIVESTACK_LISTEN_HOST", "0.0.0.0"), port=config.get("port", 8211))


if __name__ == "__main__":
    main()
