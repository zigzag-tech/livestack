"""Image ingress resolving semantic requirements through the fleet planner."""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
from pathlib import Path

from fastapi import FastAPI, Body, Header, HTTPException
from livestack_node import transport

from .contract import resolve_worker, validate_request


def json_call(base: str, method: str, path: str, body=None, token=None, timeout=1200):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, _, raw = transport.dial(base, method, path, headers=headers,
                                      body=json.dumps(body).encode() if body is not None else None, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Harmony HTTP {e.code}: {e.read().decode()[:500]}") from e
    if status >= 400:
        raise RuntimeError(f"Harmony HTTP {status}: {raw.decode()[:500]}")
    return json.loads(raw)


def create_app(config=None, call=json_call):
    config = config or json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    worker_token = Path(config["worker_token_file"]).read_text().strip()
    fleet_token = Path(config["fleet_token_file"]).read_text().strip()
    app = FastAPI(title="Harmony requirement-routed image generation")

    @app.get("/health")
    def health():
        return {"ok": True, "fleet": config["fleet"]}

    @app.post("/v1/images/generations")
    def generate(body: dict = Body(...), authorization: str | None = Header(None)):
        if not secrets.compare_digest(authorization or "", f"Bearer {worker_token}"):
            raise HTTPException(401, "image ingress credential required")
        try:
            request = validate_request(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        try:
            # Observe-only fleet planner picks the unit and device; the worker's
            # local broker is the only authority allowed to load or evict there.
            grant = call(config["fleet"], "POST", "/admit", {
                "requires": request["harmony_requires"],
                "owner": config.get("owner", "acct_harmony-image")}, token=fleet_token)
            view = call(config["fleet"], "GET", "/fleet")
            target = resolve_worker(view, grant, request["harmony_requires"])
            endpoint = target["peer"].removesuffix("/livestack")
            result = call(endpoint, "POST", "/v1/images/generations", request, token=worker_token)
            actual = result.get("harmony", {})
            if actual.get("unit") != grant["kind"] or actual.get("device_id") != grant["device_id"]:
                raise RuntimeError("worker result does not match the fleet grant")
            actual["fleet_grant"] = grant
            actual["worker_endpoint"] = endpoint
            return result
        except (RuntimeError, OSError, ValueError) as e:
            raise HTTPException(503, str(e)) from e
    return app


def main():
    import uvicorn
    config = json.loads(Path(os.environ["HARMONY_IMAGE_GATEWAY_CONFIG"]).read_text())
    uvicorn.run(create_app(config), host="0.0.0.0", port=config.get("port", 8211))


if __name__ == "__main__":
    main()
