"""Thin wiring: one ingress session (ReconnectingProducer) plus the loops that drive the pure modules.

A process builds a `StreamsRuntime`, hands it the components it owns (`attach`), and `start()`s it inside
a running asyncio loop. Nothing here decides anything; the pure modules do. Failures have their own
values: a publish while the daemon is away raises `ProducerDisconnected` (logged, retried next tick, the
daemon reports the source `failed / producer_disconnected` on its side), a registration refusal such as
`authority_conflict` is logged loudly and retried at the SDK's slow cadence, and every `mode` /
`authority_lost` notice is logged.

Standard library only; relative imports only.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .benchday_streams import ProducerDisconnected, ReconnectingProducer
from .common import Clock, optional, sha256_digest, canonical
from . import fleet_facts

HOST = "harmony.host/1"
FLEET = "harmony.fleet/1"
JOBS = "harmony.jobs/1"
WORKLOAD = "harmony.workload/1"
REQUEST = "harmony.request/1"

PACKAGE = "livestack/harmony"
MAX_JOB_SUBJECTS = 256
JOB_HEARTBEAT_S = 30.0       # harmony.jobs/1 ttl is 60 s
TERMINAL_JOB_HEARTBEAT_S = 600.0


class StreamsRuntime:
    def __init__(self, socket_path: Optional[str], log: Callable[[str], None], host: str,
                 clock: Optional[Clock] = None, tick_s: float = 2.0, fleet_s: float = 5.0) -> None:
        self.socket_path, self.log, self.host = socket_path, log, host
        self.clock = optional(clock)
        self.tick_s, self.fleet_s = tick_s, fleet_s
        self.producer: Optional[ReconnectingProducer] = None
        self.modes: Dict[str, str] = {}
        self.host_facts: Any = None
        self.fleet_reader: Optional[Callable[[], Tuple[dict, dict]]] = None
        self.jobs_reader: Optional[Callable[[], List[dict]]] = None
        self.workload: Any = None
        self.request: Any = None
        self._tasks: List[asyncio.Future] = []
        self._job_last: Dict[str, Tuple[str, int]] = {}
        self.fleet_published = 0
        self._publish_lock = None
        self._last_publish_at: Optional[float] = None

    # -- publishers handed to the pure modules
    def client(self) -> Any:
        client = self.producer.client if self.producer else None
        if client is None or not client.is_open:
            raise ProducerDisconnected("no ingress session")
        return client

    async def _publish_paced(self, send: Callable[[], Awaitable[Any]]) -> Any:
        """Serialize state and intent publications under the daemon ingress frame budget."""
        if self._publish_lock is None:
            self._publish_lock = asyncio.Lock()
        async with self._publish_lock:
            loop = asyncio.get_running_loop()
            if self._last_publish_at is not None:
                delay = 0.025 - (loop.time() - self._last_publish_at)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_publish_at = loop.time()
            return await send()

    async def publish_state(self, contract: str, subject: dict, status: str, body: Optional[dict],
                            error: Optional[dict] = None) -> dict:
        return await self._publish_paced(lambda: self.client().publish(contract, subject, status, body, error))

    def host_publisher(self) -> Callable[..., Awaitable[Any]]:
        async def publish(status: str, body: Optional[dict], error: Optional[dict]) -> Any:
            return await self.publish_state(HOST, {"host": self.host}, status, body, error)
        return publish

    def intent_publisher(self, contract: str) -> Callable[[dict], Awaitable[Any]]:
        """Send a service-minted partial as an `intent_partial` frame from the serving connection.

        Publications share one 40-frame/s pace across this runtime; `rate_limited` is still retried and
        `forwarded: false` is logged, not hidden."""
        async def publish(partial: dict) -> Any:
            from .benchday_streams import IngressRefusal
            for attempt in range(5):
                try:
                    ack = await self._publish_paced(lambda: self.client().publish_intent(partial))
                    if ack.get("forwarded") is False:
                        self.log(f"streams: {contract} intent_partial for {partial['subject']['intent_id']} not forwarded")
                    return ack
                except IngressRefusal as refusal:
                    if refusal.code != "rate_limited" or attempt == 4:
                        raise
                    await asyncio.sleep(0.1 * (attempt + 1))
        return publish

    # -- lifecycle
    def attach(self, *, host_facts: Any = None, fleet_reader: Any = None, jobs_reader: Any = None,
               workload: Any = None, request: Any = None) -> "StreamsRuntime":
        self.host_facts, self.fleet_reader, self.jobs_reader = host_facts, fleet_reader, jobs_reader
        self.workload, self.request = workload, request
        return self

    def contracts(self) -> Tuple[List[str], Dict[str, Any]]:
        register: List[str] = []
        serve: Dict[str, Any] = {}
        if self.host_facts is not None:
            register.append(HOST)
        if self.fleet_reader is not None:
            register.append(FLEET)
        if self.workload is not None:
            serve[WORKLOAD] = self.workload.handle_write
        if self.request is not None:
            serve[REQUEST] = self.request.handle_write
        return register, serve

    def start(self) -> None:
        register, serve = self.contracts()
        self.producer = ReconnectingProducer(
            PACKAGE, register=register, serve=serve, path=self.socket_path, on_up=self._on_up,
            on_refused=self._on_refused, log=self.log, on_mode=self._on_mode,
            on_authority_lost=self._on_authority_lost, on_authority_epoch=self._on_authority_epoch)
        self.producer.start()
        if self.host_facts is not None:
            self._tasks.append(asyncio.ensure_future(self._loop(self.host_facts.tick, 5.0, "host_facts")))
        if self.fleet_reader is not None:
            self._tasks.append(asyncio.ensure_future(self._loop(self._fleet_tick, self.fleet_s, "fleet")))
        if self.jobs_reader is not None:
            self._tasks.append(asyncio.ensure_future(self._loop(self._jobs_tick, self.tick_s, "jobs")))
        if self.workload is not None:
            self._tasks.append(asyncio.ensure_future(self._loop(self.workload.pump, self.tick_s, "workload")))
        if self.request is not None:
            self._tasks.append(asyncio.ensure_future(self._loop(self.request.pump, self.tick_s, "request")))
        self.log(f"streams: started (register={register}, serve={sorted(serve)})")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.producer:
            await self.producer.stop()

    # -- notices
    def _on_up(self, _client: Any, epochs: Dict[str, Optional[int]]) -> None:
        self.log(f"streams: ingress up; authority epochs {epochs}")
        for contract, authority in ((WORKLOAD, self.workload), (REQUEST, self.request)):
            if authority is not None:
                epoch = epochs.get(contract)
                if isinstance(epoch, int) and epoch > 0:
                    authority.epoch = epoch
                    asyncio.ensure_future(authority.reannounce(epoch))
                else:
                    self.log(f"streams: {contract} serving without a valid authority epoch")

    def _on_authority_epoch(self, contract: str, epoch: int) -> None:
        authority = {WORKLOAD: self.workload, REQUEST: self.request}.get(contract)
        if authority is None:
            return
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            authority.epoch = 0
            self.log(f"streams: {contract} received an invalid authority epoch")
            return
        # Set the fence synchronously: a write can arrive as soon as `serve`
        # returns, before ReconnectingProducer has registered other contracts.
        authority.epoch = epoch
        if self.producer is not None and self.producer.client is not None:
            self.log(f"streams: {contract} authority epoch changed to {epoch}; re-announcing intents")
            asyncio.ensure_future(authority.reannounce(epoch))

    def _on_refused(self, stage: str, refusal: Any) -> None:
        self.log(f"streams: REFUSED at {stage}: {refusal}")

    def _on_mode(self, contract: str, mode: Any) -> None:
        self.modes[contract] = str(mode)
        self.log(f"streams: {contract} is now {mode}")

    def _on_authority_lost(self, contract: str, holder: str) -> None:
        authority = {WORKLOAD: self.workload, REQUEST: self.request}.get(contract)
        if authority is not None:
            authority.epoch = 0
        self.log(f"streams: AUTHORITY LOST for {contract}; holder now {holder or 'unknown'}")

    # -- loops
    async def _loop(self, step: Callable[[], Awaitable[Any]], every_s: float, name: str) -> None:
        failing = False
        while True:
            try:
                await step()
                if failing:
                    self.log(f"streams: {name} recovered")
                failing = False
            except asyncio.CancelledError:
                raise
            except ProducerDisconnected as error:
                if not failing:
                    self.log(f"streams: {name} cannot publish ({error}); retrying")
                failing = True
            except Exception as error:  # noqa: BLE001 - a bad tick must not end the loop, and must be visible
                self.log(f"streams: {name} tick failed: {error!r}")
                failing = True
            await asyncio.sleep(min(every_s, 0.5) if failing else every_s)

    async def _fleet_tick(self) -> None:
        loop = asyncio.get_event_loop()
        fleet, status = await loop.run_in_executor(None, self.fleet_reader)
        body = fleet_facts.fleet_body(fleet, status, self.clock() / 1000.0)
        ack = await self.publish_state(FLEET, {"node": self.host}, "ok", body)
        self.fleet_published += 1
        if ack.get("shadowed") and self.fleet_published == 1:
            self.log("streams: harmony.fleet/1 is in shadow (compared with the adapter, not emitted)")

    async def _jobs_tick(self) -> None:
        loop = asyncio.get_event_loop()
        jobs = await loop.run_in_executor(None, self.jobs_reader)
        now = self.clock()
        for job in jobs[:MAX_JOB_SUBJECTS]:
            body = fleet_facts.job_body(job, now)
            digest = sha256_digest(canonical(body))
            terminal = body["state"] in ("succeeded", "failed", "cancelled", "expired")
            last = self._job_last.get(job["id"])
            heartbeat = (TERMINAL_JOB_HEARTBEAT_S if terminal else JOB_HEARTBEAT_S) * 1000
            if last and last[0] == digest and now - last[1] < heartbeat:
                continue
            if terminal and last and last[0] == digest and now - body.get("finished_ms", now) > TERMINAL_JOB_HEARTBEAT_S * 1000:
                continue  # a long-finished job is no longer refreshed; its partial goes stale on its own
            await self.publish_state(JOBS, {"job": job["id"]}, "ok", body)
            self._job_last[job["id"]] = (digest, now)
        while len(self._job_last) > MAX_JOB_SUBJECTS:  # bound: forget the oldest-published subject
            self._job_last.pop(min(self._job_last, key=lambda k: self._job_last[k][1]))
