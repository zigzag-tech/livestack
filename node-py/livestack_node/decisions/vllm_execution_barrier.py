"""Production vLLM worker extension for extraction cessation acknowledgement.

Selected by --worker-extension-cls, not a development HTTP abort API. The
frontend aborts the tracked request, then sends this collective RPC on the
same synchronous single-engine IPC client. Its ordered scheduler abort precedes
CUDA synchronization. Deferred/async batches refuse: they could launch work
after a barrier. A timeout is NOT cessation evidence.
No requests, responses or receipts are stored; GPU imports are method-local.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets

from .contract import ContractError


class ExtractionExecutionBarrier:
    """Mixin injected into vLLM workers through its supported extension seam."""

    def harmony_extraction_synchronize(self, nonce: str) -> dict:
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ValueError("invalid extraction barrier nonce")
        import torch
        torch.cuda.synchronize(self.device)
        return {"nonce": nonce, "worker_pid": os.getpid(),
                "device": str(self.device), "synchronized": True}


async def abort_and_confirm(engine_client, request_id: str, *, workers: int,
                            timeout_seconds: float = 2.0) -> list[dict]:
    """Collect a correlated GPU receipt; retain reservations on every failure.

    Only the installed single-engine AsyncMPClient's ordered input channel is
    supported. Multi-engine routing requires a separately proven ordering
    contract. Closing a socket alone never qualifies as stopped execution.
    """
    if not isinstance(request_id, str) or not re.fullmatch(r"chatcmpl-[A-Za-z0-9_-]{1,128}", request_id):
        raise ContractError("invalid_input", "invalid tracked extraction request identity", 400)
    if type(workers) is not int or not 1 <= workers <= 8 or not 0 < timeout_seconds <= 2:
        raise ContractError("invalid_input", "invalid extraction barrier bounds", 400)
    cls = type(getattr(engine_client, "engine_core", None))
    if cls.__module__ != "vllm.v1.engine.core_client" or cls.__name__ != "AsyncMPClient":
        raise ContractError("profile_unavailable", "extraction requires proven single-engine IPC ordering", 503)
    config = getattr(engine_client, "vllm_config", None)
    scheduler = getattr(config, "scheduler_config", None)
    parallel = getattr(config, "parallel_config", None)
    if (getattr(config, "max_concurrent_batches", None) != 1
            or getattr(scheduler, "async_scheduling", None) is not False
            or any(getattr(parallel, field, None) != 1 for field in
                   ("data_parallel_size", "pipeline_parallel_size", "tensor_parallel_size"))
            or workers != 1
            or getattr(parallel, "worker_extension_cls", None) !=
               "livestack_node.decisions.vllm_execution_barrier.ExtractionExecutionBarrier"):
        raise ContractError("profile_unavailable", "extraction stop requires the qualified synchronous single-worker configuration", 503)
    nonce = secrets.token_hex(16)
    try:
        async with asyncio.timeout(timeout_seconds):
            await engine_client.abort(request_id)
            receipts = await engine_client.collective_rpc(
                "harmony_extraction_synchronize", timeout=timeout_seconds, args=(nonce,))
    except Exception as exc:
        raise ContractError("unhealthy", "extraction stop is unconfirmed; reservations must remain held", 503) from exc
    valid = (isinstance(receipts, list) and len(receipts) == workers
             and all(isinstance(row, dict) and set(row) == {"nonce", "worker_pid", "device", "synchronized"}
                 and row["nonce"] == nonce and row["synchronized"] is True
                 and type(row["worker_pid"]) is int and row["worker_pid"] > 0
                 and isinstance(row["device"], str) and re.fullmatch(r"cuda(?::[0-9]+)?", row["device"])
                 for row in receipts)
             and len({row["worker_pid"] for row in receipts}) == workers)
    if not valid:
        raise ContractError("unhealthy", "extraction stop receipt is invalid; reservations must remain held", 503)
    return receipts
