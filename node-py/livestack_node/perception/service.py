"""Authenticated Harmony perception endpoint with admitted backend dispatch."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from starlette.requests import Request

from livestack_node.fleet_auth import AuthError, authenticate

from .contract import PerceptionContractError, validate_request, validate_result


@dataclass
class InferenceControl:
    """Cooperative cancellation/deadline state shared with a provider adapter."""

    deadline_at: float | None = None
    event: threading.Event = field(default_factory=threading.Event)
    cause: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]):
        milliseconds = payload.get("limits", {}).get("deadlineMs")
        return cls(deadline_at=time.monotonic() + milliseconds / 1000 if milliseconds else None)

    def cancel(self, cause: str) -> None:
        if not self.event.is_set():
            self.cause = cause
            self.event.set()

    def cancelled(self) -> bool:
        if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
            self.cancel("deadline")
        return self.event.is_set()


class PerceptionService:
    def __init__(self, *, principals: Mapping, backends: Mapping[str, Any], admit: Callable[..., dict]):
        self.principals = principals
        self.backends = dict(backends)
        self.admit = admit

    def handle(self, payload: Mapping[str, Any], headers: Mapping[str, str | None],
               control: InferenceControl | None = None) -> tuple[int, dict]:
        request_id = str(payload.get("requestId") or "unknown")
        try:
            realm = (headers.get("x-harmony-realm") or "").strip()
            if not realm:
                raise PerceptionContractError("invalid_input", "X-Harmony-Realm is required", 403)
            try:
                owner, _principal = authenticate(
                    self.principals, headers.get("authorization"), headers.get("x-harmony-owner")
                )
            except AuthError as exc:
                cause = "unauthorized" if exc.status == 401 else "invalid_input"
                raise PerceptionContractError(cause, exc.detail, exc.status) from exc
            request = validate_request(payload)
            grant = self.admit(request=request, owner=owner, realm=realm)
            backend_name = grant.get("backend")
            backend = self.backends.get(backend_name)
            if backend is None:
                raise PerceptionContractError("unavailable", f"no worker for admitted backend {backend_name}", 503, retryable=True)
            control = control or InferenceControl.from_payload(request)
            if control.cancelled():
                raise PerceptionContractError(control.cause or "cancelled", "request cancelled before inference", 408)
            result = backend.infer(request, grant=grant, control=control)
            if control.cancelled():
                cause = control.cause or "cancelled"
                detail = "request deadline exceeded" if cause == "deadline" else "client disconnected"
                raise PerceptionContractError(cause, detail, 408)
            return 200, validate_result(result, request=request)
        except PerceptionContractError as exc:
            return exc.status, exc.envelope(request_id)
        except Exception as exc:
            return 500, PerceptionContractError("inference_failed", str(exc), 500, retryable=False).envelope(request_id)

    def direct_worker(self, payload: Mapping[str, Any]) -> tuple[int, dict]:
        request_id = str(payload.get("requestId") or "unknown")
        error = PerceptionContractError("invalid_input", "direct worker access bypasses Harmony admission", 403)
        return error.status, error.envelope(request_id)


def build_app(service: PerceptionService):
    from fastapi import Body, FastAPI, Header
    from fastapi.responses import JSONResponse

    app = FastAPI()

    active: dict[str, InferenceControl] = {}
    active_lock = threading.Lock()
    cleanup_tasks: set[asyncio.Task] = set()

    @app.post("/v1/perception")
    async def perception(
        raw_request: Request,
        payload: dict = Body(...),
        authorization: str | None = Header(default=None),
        x_harmony_realm: str | None = Header(default=None),
        x_harmony_owner: str | None = Header(default=None),
    ):
        request_id = str(payload.get("requestId") or "unknown")
        control = InferenceControl.from_payload(payload)
        with active_lock:
            active[request_id] = control
        headers = {
            "authorization": authorization,
            "x-harmony-realm": x_harmony_realm,
            "x-harmony-owner": x_harmony_owner,
        }
        task = asyncio.create_task(asyncio.to_thread(service.handle, payload, headers, control))
        detached = False
        try:
            while not task.done():
                if control.cancelled():
                    break
                if await raw_request.is_disconnected():
                    control.cancel("cancelled")
                    break
                await asyncio.sleep(0.025)
            if task.done():
                status, body = await task
            else:
                # Do not make the caller wait for provider cleanup after its
                # deadline or disconnect. Keep the task and active control
                # registered until cleanup actually finishes.
                try:
                    status, body = await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                except asyncio.TimeoutError:
                    detached = True
                    cause = control.cause or "cancelled"
                    detail = "request deadline exceeded" if cause == "deadline" else "client disconnected"
                    error = PerceptionContractError(cause, detail, 408)
                    status, body = error.status, error.envelope(request_id)

                    async def finish_cleanup():
                        try:
                            await task
                        finally:
                            with active_lock:
                                active.pop(request_id, None)
                    cleanup_task = asyncio.create_task(finish_cleanup())
                    cleanup_tasks.add(cleanup_task)
                    cleanup_task.add_done_callback(cleanup_tasks.discard)
        finally:
            if not detached:
                with active_lock:
                    active.pop(request_id, None)
        return JSONResponse(body, status_code=status)

    @app.post("/v1/perception/{request_id}/cancel")
    def cancel(
        request_id: str,
        authorization: str | None = Header(default=None),
        x_harmony_realm: str | None = Header(default=None),
        x_harmony_owner: str | None = Header(default=None),
    ):
        # Reuse normal service authentication without admitting or dispatching.
        try:
            realm = (x_harmony_realm or "").strip()
            if not realm:
                raise PerceptionContractError("invalid_input", "X-Harmony-Realm is required", 403)
            authenticate(service.principals, authorization, x_harmony_owner)
        except AuthError as exc:
            return JSONResponse({"cause": "unauthorized", "detail": exc.detail}, status_code=exc.status)
        except PerceptionContractError as exc:
            return JSONResponse(exc.envelope(request_id), status_code=exc.status)
        with active_lock:
            control = active.get(request_id)
        if control is None:
            return JSONResponse({"requestId": request_id, "cancelled": False}, status_code=404)
        control.cancel("cancelled")
        return {"requestId": request_id, "cancelled": True}

    @app.post("/v1/worker/perception")
    def direct_worker(payload: dict = Body(...)):
        status, body = service.direct_worker(payload)
        return JSONResponse(body, status_code=status)

    app.state.perception_service = service
    app.state.perception_active = active
    app.state.perception_cleanup_tasks = cleanup_tasks
    return app
