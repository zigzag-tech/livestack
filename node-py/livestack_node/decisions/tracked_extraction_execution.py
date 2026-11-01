"""Bounded frontend ownership for the opt-in vLLM extraction endpoint.

The endpoint must supply the same engine request id to its chat renderer.
No runtime is installed here. A single retained task bounds quarantine memory;
an unconfirmed stop refuses further work instead of accumulating requests.
"""
import asyncio
import logging
import re

from .contract import ContractError
from .execution_uncertain import ExecutionUnconfirmed
from .vllm_execution_barrier import abort_and_confirm, validate_execution_configuration


class TrackedExtractionExecution:
    def __init__(self, engine_client):
        self.engine_client = engine_client
        self.active = None

    async def execute(self, request_id, start, *, timeout_seconds):
        if not isinstance(request_id, str) or not re.fullmatch(r"chatcmpl-[A-Za-z0-9_-]{1,128}", request_id):
            raise ContractError("invalid_input", "invalid extraction execution identity", 400)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 8:
            raise ContractError("invalid_input", "extraction execution deadline exceeds eight seconds", 400)
        if self.active is not None:
            raise ContractError("capacity", "prior extraction execution still owns its slot", 429)
        validate_execution_configuration(self.engine_client)
        # No await between the capacity check and ownership registration.
        task = asyncio.create_task(start(request_id))
        self.active = (request_id, task)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
            if not done:
                await self._protected_stop(request_id, task)
                raise ContractError("deadline_exceeded", "tracked extraction expired after confirmed cessation", 504)
            try:
                result = task.result()
            except BaseException:
                await self._protected_stop(request_id, task)
                raise
            self.active = None
            return result
        except asyncio.CancelledError:
            if self.active is not None:
                await self._protected_stop(request_id, task)
            raise

    async def _protected_stop(self, request_id, task):
        # Repeated caller cancellation cannot cancel the bounded stop attempt.
        stop = asyncio.create_task(self._stop(request_id, task))
        while not stop.done():
            try:
                await asyncio.shield(stop)
            except asyncio.CancelledError:
                continue
        return stop.result()

    async def _stop(self, request_id, task):
        try:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=2)
            if not done:
                raise ExecutionUnconfirmed("unhealthy", "frontend cancellation unconfirmed; execution slot retained", 503)
            # A finished renderer cannot enqueue generation after the barrier.
            try:
                task.result()
            except BaseException:
                pass  # Its terminal outcome is superseded by cessation evidence.
            receipts = await abort_and_confirm(self.engine_client, request_id, workers=1)
        except ExecutionUnconfirmed:
            logging.error("extraction frontend stop unconfirmed; bounded execution slot retained")
            raise
        except Exception as exc:
            logging.error("extraction GPU stop unconfirmed; bounded execution slot retained")
            raise ExecutionUnconfirmed("unhealthy", "GPU cessation unconfirmed; execution slot retained", 503) from exc
        self.active = None
        return receipts
