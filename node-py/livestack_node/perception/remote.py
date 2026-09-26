"""Harmony-to-Harmony forwarding for accelerator workers on another host."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
from pathlib import Path

from livestack_node import transport

from .contract import PerceptionContractError


def read_token(path: str) -> str:
    value = Path(path).read_text().strip()
    if "=" in value and "\n" not in value:
        value = value.split("=", 1)[1].strip().strip("'\"")
    if not value:
        raise RuntimeError(f"empty Harmony remote token file: {path}")
    return value


class RemotePerceptionAdapter:
    """Forward one validated request to another admitted Harmony ingress."""

    def __init__(self, *, url: str, token: str, timeout: float = 900):
        self.url = url
        self.token = token
        self.timeout = timeout

    def infer(self, request: dict, *, grant: dict, control=None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Harmony-Realm": grant["realm"],
            "X-Harmony-Owner": grant["owner"],
        }
        target, path = transport.split_target(self.url)
        finished = threading.Event()
        def propagate_cancel():
            if control is None:
                return
            while not finished.wait(0.025):
                if not control.cancelled():
                    continue
                cancel_target, cancel_path = transport.split_target(
                    self.url.rstrip("/") + f"/{request['requestId']}/cancel")
                for _ in range(3):
                    try:
                        transport.dial(cancel_target, "POST", cancel_path,
                                       headers=headers, body=b"{}",
                                       timeout=5)
                        return
                    except Exception:
                        time.sleep(0.05)
                return
        monitor = threading.Thread(target=propagate_cancel, daemon=True)
        monitor.start()
        try:
            with transport.dial_stream(target, "POST", path, headers=headers,
                                       body=json.dumps(request).encode(),
                                       timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except Exception:
                body = {}
            if control is not None and control.cancelled() and control.cause == "deadline":
                raise PerceptionContractError(
                    "deadline", "request deadline exceeded", 408, retryable=False) from exc
            raise PerceptionContractError(
                body.get("cause", "unavailable"),
                body.get("detail", f"remote Harmony ingress returned HTTP {exc.code}"),
                exc.code, retryable=bool(body.get("retryable", exc.code >= 500))) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PerceptionContractError(
                "unavailable", f"remote Harmony ingress failed: {exc}", 503,
                retryable=True) from exc
        finally:
            finished.set()


def load_remote_routes(path: str | None) -> list[dict]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text())
    routes = payload.get("routes", [])
    for route in routes:
        required = {"name", "url", "tokenFile", "match"}
        if not required.issubset(route):
            raise RuntimeError(f"invalid Harmony perception remote route: {route}")
    return routes


def matching_route(routes: list[dict], requirements: dict) -> dict | None:
    backend, model = requirements.get("backend"), requirements.get("model")
    if backend is None and model is None:
        return None
    for route in routes:
        match = route["match"]
        if backend is not None and backend not in match.get("backends", []):
            continue
        if model is not None and model not in match.get("models", []):
            continue
        return route
    return None
