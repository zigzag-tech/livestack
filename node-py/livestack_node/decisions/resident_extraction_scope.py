"""Protect a resident extraction engine with the serving facade's real gates.

This context holds no history or private queue. It borrows an immediate slot
from the facade's existing UnitQueue, contributes to its node busy counter and
sets the same managed-unit busy flag as the chat proxy. Cold work is a named
refusal: no unadmitted model load or background thread is started here.
"""
from contextlib import asynccontextmanager

from .contract import ContractError


class ResidentExtractionScope:
    def __init__(self, *, manager, queues, busy, max_concurrent, engine_is_up,
                 device_id):
        self.manager = manager
        self.queues = queues
        self.busy = busy
        self.max_concurrent = max_concurrent
        self.engine_is_up = engine_is_up
        self.device_id = device_id

    @asynccontextmanager
    async def __call__(self, payload, grant, owner, realm):
        del payload
        name = grant.kind
        if not owner or not realm or not grant.granted or grant.device_id != self.device_id:
            raise ContractError("forbidden", "resident extraction admission scope does not match this node", 403)
        if name not in self.manager.units or name not in self.manager.resident:
            raise ContractError("profile_unavailable", "qualified extraction engine is not resident", 503)
        if not self.engine_is_up(name):
            raise ContractError("unhealthy", "resident extraction engine is not ready", 503)
        limit = self.max_concurrent(name)
        slot = self.queues.try_acquire(name, limit)
        if slot is None:
            raise ContractError("capacity", "qualified extraction engine has no immediate capacity", 429)
        unit = self.manager.units[name]
        counted = False
        try:
            self.busy.acquire()
            counted = True
            unit.busy = True
            self.manager.touch()
            self.manager.coordinator.report_busy(name, True)
            # Residency/readiness may have changed immediately before the busy
            # edge. Never infer against a vanished handle after claiming it.
            if name not in self.manager.resident or not self.engine_is_up(name):
                raise ContractError("unhealthy", "extraction engine vanished before dispatch", 503)
            yield
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError("unhealthy", "resident extraction execution failed", 503) from exc
        finally:
            slot.release()
            if counted:
                self.busy.release()
            if self.queues.status(name, limit)["in_flight"] == 0:
                unit.busy = False
