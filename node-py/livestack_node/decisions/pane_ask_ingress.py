"""Facade route for qualified pane-ask extraction, with mandatory admission.

The service owner installs a runtime only after qualification and broker wiring
are verified. Missing deployment prerequisites return a named refusal. This
route never delegates to the legacy chat/admit degrade-to-grant path.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from .admission import Admission
from .auth import authenticate_decision
from .contract import ContractError, MAX_BODY_BYTES, validate_request
from .extraction_contract import PROFILE_ID
from .nominal_27b_adapter import Nominal27BExtractionAdapter
from .resident_extraction_scope import ResidentExtractionScope


@dataclass(frozen=True)
class PaneAskRuntime:
    adapter: Nominal27BExtractionAdapter
    admit: Callable[..., Awaitable[Admission]]
    finish: Callable[[Admission], Awaitable[None]]


def install_pane_ask_route(app, principals_supplier):
    @app.post("/v1/decisions")
    async def decisions(request: Request):
        request_id = ""
        grant = None
        runtime = None
        try:
            # Authenticate before reading a potentially unbounded stream.
            owner, realm, _ = authenticate_decision(principals_supplier() or {},
                authorization=request.headers.get("authorization"),
                realm=request.headers.get("x-harmony-realm"),
                owner_header=request.headers.get("x-harmony-owner"))
            raw = bytearray()
            try:
                async with asyncio.timeout(8):
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > MAX_BODY_BYTES:
                            raise ContractError("invalid_input", "decision request exceeds 64 KiB", 400)
                        raw.extend(chunk)
            except TimeoutError as exc:
                raise ContractError("deadline_exceeded", "decision request body deadline exceeded", 504) from exc
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("body must be an object")
            except (ValueError, UnicodeError) as exc:
                raise ContractError("invalid_input", "decision body must be a JSON object", 400) from exc
            request_id = str(payload.get("request_id") or "")[:128]
            runtime = getattr(app.state, "pane_ask_runtime", None)
            # Validation precedes a missing-runtime refusal, so malformed and
            # expired requests retain their contract cause.
            now = runtime.adapter.now_ms() if isinstance(runtime, PaneAskRuntime) else int(time.time() * 1000)
            validate_request(payload, now_ms=now)
            if payload["profile_id"] != PROFILE_ID or payload["questions"][0]["type"] != "extraction":
                raise ContractError("unsupported_profile", "facade serves only pinned pane-ask extraction", 422)
            if not isinstance(runtime, PaneAskRuntime):
                raise ContractError("profile_unavailable", "qualified pane-ask runtime is not installed", 503)
            scope = getattr(app.state, "pane_ask_execution_scope", None)
            if not isinstance(scope, ResidentExtractionScope):
                raise ContractError("profile_unavailable", "resident extraction protection is not installed", 503)
            runtime.adapter.engine.validate()
            remaining = payload["deadline_at_ms"] - runtime.adapter.now_ms()
            if remaining > 8000:
                raise ContractError("invalid_input", "pane-ask deadline exceeds eight seconds", 400)
            try:
                candidate = await asyncio.wait_for(runtime.admit(owner=owner, realm=realm,
                    profile_id=PROFILE_ID, request_id=payload["request_id"],
                    deadline_at_ms=payload["deadline_at_ms"]), max(0, remaining) / 1000)
            except TimeoutError as exc:
                raise ContractError("deadline_exceeded", "decision admission deadline exceeded", 504) from exc
            except ContractError:
                raise
            except Exception as exc:
                raise ContractError("broker_unavailable", "decision admission failed; new work refused", 503) from exc
            if not isinstance(candidate, Admission):
                raise ContractError("broker_unavailable", "decision admission returned no valid grant", 503)
            if not candidate.granted:
                raise ContractError("capacity", "decision admission refused new work", 429)
            grant = candidate
            async with scope(payload, grant, owner, realm):
                result = await runtime.adapter.infer(payload, admission=grant)
            return JSONResponse(result)
        except ContractError as exc:
            return JSONResponse(exc.envelope(request_id), status_code=exc.http_status)
        finally:
            if grant is not None:
                # Release failure propagates instead of pretending a lease was
                # closed. The facade's exception instrumentation records it.
                await asyncio.wait_for(runtime.finish(grant), timeout=1)
