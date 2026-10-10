# VENDORED UNCHANGED from Benchday packages/benchday-plugin-api/sdk/python/benchday_streams/client.py
# Benchday main commit 05eb4c9de (intent authority fencing and reconnect callbacks).
# Re-vendor from Benchday; do not edit this copy.
"""Local-ingress client: publish, register as a native producer, serve and publish intent outcomes.

Frames are 4-byte big-endian length-prefixed JSON of at most 64 KiB over the daemon's 0600 unix
socket. Every client frame is answered by exactly one reply, in order, except `write_result`; the
daemon also sends unsolicited `lease`, `mode`, `write`, `authority_lost` and `authority_epoch` frames. See
openspec/changes/services-own-their-streams/design.md (D4, D5).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

MAX_FRAME_BYTES = 65536
MAX_PENDING_REQUESTS = 256
MAX_INFLIGHT_WRITES = 256
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class IngressRefusal(Exception):
    """The daemon refused a frame; `code` is stable (for example `authority_conflict`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ProducerDisconnected(Exception):
    """The ingress session is gone. A native producer that is disconnected is reported FAILED by the
    daemon (`producer_disconnected`); the SDK never pretends the publish happened."""


@dataclass(frozen=True)
class WriteFrame:
    req: str
    contract: str
    op: str
    intent_id: str
    principal: str
    payload: dict


def ingress_socket_path(env: Optional[dict] = None) -> Optional[str]:
    env = os.environ if env is None else env
    state = env.get("XDG_STATE_HOME") or (os.path.join(env["HOME"], ".local", "state") if env.get("HOME") else None)
    return os.path.join(state, "benchday-daemon", "streams-ingress.sock") if state else None


def encode_frame(value: Any) -> bytes:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError("ingress frame exceeds 64 KiB")
    return struct.pack(">I", len(body)) + body


def reference(ref: str, digest: str, nbytes: int, media: str) -> dict:
    """A reference-typed field (umbrella D6): large I/O travels by reference, never inline."""
    if not ref or len(ref) > 512:
        raise ValueError("reference: ref must be 1..512 characters")
    if not _DIGEST.match(digest or ""):
        raise ValueError("reference: digest must be sha256:<64 hex>")
    if not isinstance(nbytes, int) or isinstance(nbytes, bool) or nbytes < 1:
        raise ValueError("reference: bytes must be a positive integer")
    if not media or len(media) > 128:
        raise ValueError("reference: media must be 1..128 characters")
    return {"ref": ref, "digest": digest, "bytes": nbytes, "media": media}


def intent_partial(contract: str, intent_id: str, source: str, epoch: int, seq: int, status: str,
                   body: Optional[dict] = None, now_ms: Optional[int] = None) -> dict:
    """The partial an intent authority answers a write with. The service owns epoch and seq."""
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ValueError("intent partial epoch must be a positive integer")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        raise ValueError("intent partial seq must be a positive integer")
    return {
        "contract": contract, "source": source, "subject": {"intent_id": intent_id},
        "epoch": epoch, "seq": seq, "produced_ms": int(time.time() * 1000) if now_ms is None else now_ms,
        "ttl_ms": 0, "status": "ok", "body": {"intent_id": intent_id, "status": status, **(body or {})},
    }


WriteHandler = Callable[[WriteFrame], Awaitable[dict]]


class IngressClient:
    """One ingress session. Create with `await IngressClient.open(...)`."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, package: str, **events: Any) -> None:
        self._reader, self._writer = reader, writer
        self._package = package
        self._pending: "asyncio.Queue[asyncio.Future]" = asyncio.Queue()
        self._waiting: list = []
        self._writers: dict[str, WriteHandler] = {}
        self._authority_epochs: dict[str, int] = {}
        self._inflight_writes = 0
        self._events = events  # lifecycle callbacks; all are optional
        self._closed = False
        self._task = asyncio.ensure_future(self._read_loop())
        self.daemon = ""

    @classmethod
    async def open(cls, package: str, path: Optional[str] = None, version: str = "1", **events: Any) -> "IngressClient":
        path = path or ingress_socket_path()
        if not path:
            raise ProducerDisconnected("ingress socket path unavailable")
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except OSError as error:
            raise ProducerDisconnected(f"ingress unavailable: {error}") from error
        client = cls(reader, writer, package, **events)
        answer = await client._request({"type": "hello", "package": package, "version": version})
        client.daemon = str(answer.get("daemon", ""))
        return client

    @property
    def is_open(self) -> bool:
        return not self._closed

    async def _read_loop(self) -> None:
        error: Exception = ProducerDisconnected("ingress socket closed")
        try:
            while True:
                header = await self._reader.readexactly(4)
                (length,) = struct.unpack(">I", header)
                if length > MAX_FRAME_BYTES:
                    error = ValueError("ingress frame exceeds 64 KiB")
                    break
                row = json.loads(await self._reader.readexactly(length))
                self._dispatch(row)
        except asyncio.IncompleteReadError:
            pass
        except Exception as caught:  # noqa: BLE001 - surfaced through on_fatal and every pending request
            error = caught
        self._fail(error)

    def _dispatch(self, row: dict) -> None:
        kind = row.get("type")
        if kind == "lease" and self._events.get("on_lease"):
            self._events["on_lease"](row["contract"], bool(row.get("live")))
        elif kind == "mode" and self._events.get("on_mode"):
            self._events["on_mode"](row["contract"], row.get("mode"))
        elif kind == "authority_lost":
            contract = str(row.get("contract", ""))
            self._authority_epochs.pop(contract, None)
            if self._events.get("on_authority_lost"):
                self._events["on_authority_lost"](contract, str(row.get("holder", "")))
            # The daemon retired this serve registration. Reconnect and run the
            # same register/serve path again so a conflict remains visible and
            # can recover after the holder disappears.
            self._fail(ProducerDisconnected(f"authority lost for {contract}"))
            return
        elif kind == "authority_epoch":
            contract = str(row.get("contract", ""))
            epoch = row.get("epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
                self._fail(ProducerDisconnected(f"invalid authority epoch for {contract}"))
                return
            self._authority_epochs[contract] = epoch
            if self._events.get("on_authority_epoch"):
                self._events["on_authority_epoch"](contract, epoch)
            return
        elif kind == "write":
            if self._inflight_writes >= MAX_INFLIGHT_WRITES:
                self._fail(ProducerDisconnected("ingress write concurrency limit"))
                return
            self._inflight_writes += 1
            asyncio.ensure_future(self._answer_write(WriteFrame(
                row["req"], row["contract"], row["op"], row["intent_id"], row.get("principal", ""), row.get("payload") or {})))
        elif kind in ("lease", "mode"):
            return
        else:
            if not self._waiting:
                self._fail(ProducerDisconnected(f"ingress frame without a request: {kind}"))
                return
            future = self._waiting.pop(0)
            if future.done():
                return
            if kind == "error":
                future.set_exception(IngressRefusal(str(row.get("code", "ingress_error")), str(row.get("detail", ""))))
            else:
                future.set_result(row)

    async def _answer_write(self, write: WriteFrame) -> None:
        try:
            handler = self._writers.get(write.contract)
            if handler is None:
                raise RuntimeError("no_handler")
            partial = await handler(write)
        except Exception as error:  # noqa: BLE001 - an unhandled write is answered, never dropped
            epoch = self._authority_epochs.get(write.contract)
            if epoch is None:
                self._fail(ProducerDisconnected(f"serve epoch unavailable for {write.contract}"))
                return
            partial = intent_partial(write.contract, write.intent_id, "sdk", epoch, 1, "refused",
                                     {"refusal": "authority_unavailable", "detail": f"handler_failed:{error}"[:240]})
        finally:
            self._inflight_writes -= 1
        try:
            self._send({"type": "write_result", "req": write.req, "partial": partial})
        except Exception as error:  # noqa: BLE001
            self._fail(error)

    def _fail(self, error: Exception) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        for future in self._waiting:
            if not future.done():
                future.set_exception(ProducerDisconnected(str(error)))
        self._waiting.clear()
        if self._events.get("on_fatal"):
            self._events["on_fatal"](error)

    def _send(self, frame: dict) -> None:
        if self._closed:
            raise ProducerDisconnected("ingress socket unavailable")
        self._writer.write(encode_frame(frame))

    async def _request(self, frame: dict) -> dict:
        if self._closed:
            raise ProducerDisconnected("ingress socket unavailable")
        if len(self._waiting) >= MAX_PENDING_REQUESTS:
            raise IngressRefusal("rate_limited", "sdk_pending_request_limit")
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiting.append(future)
        try:
            self._send(frame)
        except Exception:
            self._waiting.remove(future)
            raise
        return await future

    async def publish(self, contract: str, subject: dict, status: str = "ok", body: Any = None,
                      error: Optional[dict] = None) -> dict:
        frame: dict = {"type": "publish", "contract": contract, "subject": subject, "status": status}
        if body is not None:
            frame["body"] = body
        if error is not None:
            frame["error"] = error
        answer = await self._request(frame)
        return {"key": answer.get("key", ""), "leased": answer.get("leased") is True,
                "delivered": int(answer.get("delivered", 0)), "epoch": str(answer.get("epoch", "0")),
                "seq": str(answer.get("seq", "0")), "shadowed": answer.get("shadowed") is True}

    async def publish_intent(self, partial: dict) -> dict:
        """Publish a later lifecycle partial for an intent contract served by this session."""
        contract = partial.get("contract") if isinstance(partial, dict) else None
        if not isinstance(contract, str) or not contract or contract not in self._writers:
            raise IngressRefusal("authority_unavailable", "intent_authority_not_served_by_session")
        if partial.get("epoch") != self._authority_epochs.get(contract):
            raise IngressRefusal("authority_unavailable", "intent_partial_authority_epoch_stale")
        answer = await self._request({"type": "intent_partial", "contract": contract, "partial": partial})
        return {"contract": str(answer.get("contract", contract)), "leased": answer.get("leased") is True,
                "delivered": int(answer.get("delivered", 0)), "epoch": str(answer.get("epoch", "0")),
                "seq": str(answer.get("seq", "0"))}

    async def register(self, contract: str, shadow: bool = False) -> dict:
        """Register as the native producer of a state contract; returns {contract, mode, epoch}."""
        frame: dict = {"type": "register", "contract": contract}
        if shadow:
            frame["mode"] = "shadow"
        answer = await self._request(frame)
        return {"contract": contract, "mode": "active" if answer.get("mode") == "active" else "shadow",
                "epoch": answer.get("epoch") if isinstance(answer.get("epoch"), int) else None}

    async def serve(self, contract: str, handler: WriteHandler) -> Optional[int]:
        """Serve an intent contract; returns the authority epoch used by its lifecycle partials."""
        self._writers[contract] = handler
        try:
            answer = await self._request({"type": "serve", "contract": contract})
        except Exception:
            self._writers.pop(contract, None)
            raise
        if answer.get("type") != "serving":
            self._writers.pop(contract, None)
            raise IngressRefusal("serve_refused", str(answer.get("type")))
        epoch = answer.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            self._writers.pop(contract, None)
            raise IngressRefusal("serve_refused", "authority_epoch_missing")
        self._authority_epochs[contract] = epoch
        if self._events.get("on_authority_epoch"):
            self._events["on_authority_epoch"](contract, epoch)
        return epoch

    async def close(self) -> None:
        self._closed = True
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
        self._task.cancel()


class ReconnectingProducer:
    """Keeps a session alive across daemon restarts: reopen with backoff (0.25 s doubling to 10 s),
    register and serve again. A refusal (for example `authority_conflict`) is surfaced through
    `on_refused` and retried at the slow cadence, so a refused second service stays visibly refused."""

    def __init__(self, package: str, register: Optional[list] = None, serve: Optional[dict] = None,
                 path: Optional[str] = None, on_up: Optional[Callable] = None,
                 on_refused: Optional[Callable] = None, log: Callable[[str], None] = lambda _m: None,
                 **events: Any) -> None:
        self.package, self.path = package, path
        self.register, self.serve = register or [], serve or {}
        self.on_up, self.on_refused, self.log, self.events = on_up, on_refused, log, events
        self.client: Optional[IngressClient] = None
        self._stopped = False
        self._attempt = 0
        self._task: Optional[asyncio.Task] = None
        self._down = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._down.set()
        if self.client:
            await self.client.close()
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stopped:
            slow = False
            try:
                self._down.clear()
                client = await IngressClient.open(
                    self.package, self.path, on_fatal=lambda _e: self._down.set(), **self.events)
                epochs: dict = {}
                try:
                    for contract in self.register:
                        epochs[contract] = (await client.register(contract))["epoch"]
                    for contract, handler in self.serve.items():
                        epochs[contract] = await client.serve(contract, handler)
                except IngressRefusal as refusal:
                    if self.on_refused:
                        self.on_refused("registration", refusal)
                    await client.close()
                    slow = True
                    raise
                self.client, self._attempt = client, 0
                if self.on_up:
                    self.on_up(client, epochs)
                await self._down.wait()
                self.client = None
                self.log("ingress down; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.log(f"ingress unavailable: {error}")
            if self._stopped:
                return
            self._attempt += 1
            await asyncio.sleep(10.0 if slow else min(10.0, 0.25 * 2 ** min(self._attempt, 6)))
