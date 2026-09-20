"""Authenticated POST /v1/decisions.

This is a small module on purpose. Do not grow examples/harmony-llm/server.py.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Mapping, Optional

from .admission import BrokerUnavailable, DecisionAdmission
from .auth import authenticate_decision, refuse_unauthenticated_legacy_admit
from .contract import CAUSE_HTTP, ContractError, SCHEMA_VERSION, validate_request
from .kinds import CUDA_KIND, MLX_KIND, PhysicalKind


def default_kinds(profile_id: str) -> list[PhysicalKind]:
    shared = dict(
        profile_id=profile_id,
        language="multilingual",
        max_len=1024,
        head_max_len=256,
        calibration_hash="unqualified",
        resident_bytes=3 * 1024 ** 3,
        peak_bytes=4 * 1024 ** 3,
        load_headroom_bytes=int(0.2 * 4 * 1024 ** 3),
        healthy=True,
    )
    return [
        PhysicalKind(kind=CUDA_KIND, backend="cuda", device_id="gpu-cuda-test", **shared),
        PhysicalKind(kind=MLX_KIND, backend="mlx", device_id="gpu-mlx-test", **shared),
    ]


class DecisionService:
    def __init__(
        self,
        *,
        principals: Mapping,
        backends: Dict[str, Any],
        broker_admit: Callable[..., dict],
        kinds: Optional[list] = None,
        now_ms: Optional[Callable[[], int]] = None,
        profile_id: str = "pane-attention-v1:unqualified",
    ):
        self.principals = principals
        self.backends = backends
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.admission = DecisionAdmission(
            broker_admit=broker_admit,
            kinds=kinds or default_kinds(profile_id),
            now_ms=self.now_ms,
        )
        self.legacy_admit_grants = 0

    def handle(self, payload: Mapping[str, Any], headers: Mapping[str, Optional[str]]) -> tuple[int, dict]:
        request_id = str(payload.get("request_id") or "")
        try:
            owner, realm, _principal = authenticate_decision(
                self.principals,
                authorization=headers.get("authorization"),
                realm=headers.get("x-harmony-realm"),
                owner_header=headers.get("x-harmony-owner"),
                body=payload,
            )
            del realm
            req = validate_request(dict(payload), now_ms=self.now_ms())
            backend_constraint = headers.get("x-harmony-backend") or None
            grant = self.admission.admit(
                profile_id=req["profile_id"],
                owner=owner,
                request_id=req["request_id"],
                backend=backend_constraint,
            )
            worker = self.backends.get(grant.kind)
            if worker is None:
                raise ContractError("profile_unavailable", f"no worker for {grant.kind}", 503)
            result = worker.infer(req, now_ms=self.now_ms())
            result.setdefault("execution", {})["backend"] = grant.backend
            self.admission.finish(req["request_id"])
            return 200, result
        except ContractError as e:
            return e.http_status, e.envelope(request_id)

    def legacy_admit(self, payload: Mapping[str, Any], headers: Mapping[str, Optional[str]]) -> tuple[int, dict]:
        """Unauthenticated legacy /admit must not grant decision execution."""
        try:
            refuse_unauthenticated_legacy_admit(headers.get("authorization"))
        except ContractError as e:
            return e.http_status, e.envelope("")
        # Even with a token, this path is not the decision admission path.
        return 403, {
            "schema_version": SCHEMA_VERSION,
            "request_id": "",
            "outcome": "error",
            "cause": "forbidden",
            "http_status": 403,
            "detail": "legacy /admit cannot admit typed-decision work",
        }

    def direct_worker(self, payload: Mapping[str, Any], headers: Mapping[str, Optional[str]]) -> tuple[int, dict]:
        """Calling a worker URL without scoped admission is refused before inference."""
        try:
            authenticate_decision(
                self.principals,
                authorization=headers.get("authorization"),
                realm=headers.get("x-harmony-realm"),
                owner_header=headers.get("x-harmony-owner"),
                body=payload,
            )
        except ContractError as e:
            return e.http_status, e.envelope(str(payload.get("request_id") or ""))
        return 403, {
            "schema_version": SCHEMA_VERSION,
            "request_id": str(payload.get("request_id") or ""),
            "outcome": "error",
            "cause": "forbidden",
            "http_status": 403,
            "detail": "direct worker access is not a decision admission",
        }


def build_app(service: DecisionService):
    from fastapi import Body, FastAPI, Header
    from fastapi.responses import JSONResponse

    app = FastAPI()

    def _headers(authorization, realm, owner, backend=None):
        return {
            "authorization": authorization,
            "x-harmony-realm": realm,
            "x-harmony-owner": owner,
            "x-harmony-backend": backend,
        }

    @app.post("/v1/decisions")
    async def decisions(
        payload: dict = Body(...),
        authorization: str | None = Header(default=None),
        x_harmony_realm: str | None = Header(default=None),
        x_harmony_owner: str | None = Header(default=None),
        x_harmony_backend: str | None = Header(default=None),
    ):
        status, body = service.handle(payload, _headers(
            authorization, x_harmony_realm, x_harmony_owner, x_harmony_backend,
        ))
        return JSONResponse(body, status_code=status)

    @app.post("/admit")
    async def legacy_admit(
        payload: dict | None = Body(default=None),
        authorization: str | None = Header(default=None),
    ):
        status, body = service.legacy_admit(payload or {}, _headers(authorization, None, None))
        return JSONResponse(body, status_code=status)

    @app.post("/v1/worker/decisions")
    async def direct_worker(
        payload: dict = Body(...),
        authorization: str | None = Header(default=None),
        x_harmony_realm: str | None = Header(default=None),
        x_harmony_owner: str | None = Header(default=None),
    ):
        status, body = service.direct_worker(payload, _headers(
            authorization, x_harmony_realm, x_harmony_owner,
        ))
        return JSONResponse(body, status_code=status)

    app.state.decision_service = service
    return app
