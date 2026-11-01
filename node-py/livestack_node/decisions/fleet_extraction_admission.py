"""Strict fleet lease callbacks for the qualified extraction runtime.

Uses the existing fleet/lease wire API, never the client's degrade-to-grant
/admit helper. One broker and one registry-qualified target are configured by
the service owner. No retry, placement fallback or per-request history lives
here. Broker leases expire if a caller crashes before release.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx

from .admission import Admission
from .contract import ContractError, MAX_BODY_BYTES
from .extraction_contract import PROFILE_ID
from .nominal_27b_adapter import QualifiedEngine


class FleetExtractionAdmission:
    def __init__(self, *, client: httpx.AsyncClient, broker_url: str,
                 token: str, engine: QualifiedEngine, target_id: str,
                 host_id: str, now_ms=None):
        if not token or not broker_url or not target_id or not host_id:
            raise ContractError("profile_unavailable", "extraction fleet configuration is incomplete", 503)
        self.client = client
        self.broker_url = broker_url.rstrip("/")
        self._token = token
        self.engine = engine
        self.target_id = target_id.rstrip("/")
        self.host_id = host_id
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def admit(self, *, owner: str, realm: str, profile_id: str,
                    request_id: str, deadline_at_ms: int) -> Admission:
        self.engine.validate()
        if profile_id != PROFILE_ID or not owner or not realm or not request_id:
            raise ContractError("invalid_input", "extraction admission scope is incomplete", 400)
        remaining = deadline_at_ms - self.now_ms()
        if remaining <= 0:
            raise ContractError("deadline_exceeded", "extraction expired before fleet admission", 504)
        result = await self._post("/fleet/admit", {
            "kind": self.engine.unit, "owner": owner, "sla": "interactive",
            "estimate": {"duration_s": remaining / 1000},
            # Only this registry-qualified implementation may serve. This is
            # capability eligibility; the fleet still makes the grant.
            "selector": {"host_id": self.host_id, "device_id": self.engine.device_id},
        }, timeout=remaining / 1000, realm=realm)
        if result.get("granted") is not True:
            raise ContractError("capacity", "fleet refused extraction admission", 429)
        lease_id = result.get("lease_id")
        if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 256:
            # Fleet hosted_checkout failures may return granted=True without
            # a lease. That answer cannot reserve typed-decision execution.
            raise ContractError("broker_unavailable", "fleet grant has no usable lease", 503)
        target = result.get("target")
        valid = (isinstance(target, dict) and result.get("kind") == self.engine.unit
                 and target.get("target_id") == self.target_id
                 and target.get("host_id") == self.host_id
                 and target.get("device_id") == self.engine.device_id
                 and not result.get("degraded"))
        grant = Admission(True, self.engine.unit, self.engine.backend,
                          self.engine.device_id, False, lease_id)
        if not valid:
            # An unexpected target never receives inference, and its real
            # reservation is handed back rather than left until TTL expiry.
            await self.finish(grant)
            raise ContractError("profile_unavailable", "fleet target differs from qualified registry", 503)
        return grant

    async def finish(self, grant: Admission) -> None:
        result = await self._post(f"/lease/{quote(grant.lease_id, safe='')}/release", {}, timeout=1)
        if result.get("ok") is not True:
            raise ContractError("broker_unavailable", "fleet did not acknowledge extraction lease release", 503)

    async def _post(self, path: str, body: dict, *, timeout: float, realm: str | None = None) -> dict:
        headers = {"authorization": "Bearer " + self._token}
        if realm:
            headers["x-harmony-realm"] = realm
        try:
            async with self.client.stream("POST", self.broker_url + path,
                    json=body, headers=headers, timeout=timeout) as response:
                if response.status_code == 429:
                    raise ContractError("capacity", "fleet account or capacity quota refused extraction", 429)
                if response.status_code in (401, 403):
                    raise ContractError("broker_unavailable", "fleet rejected service admission credential", 503)
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_BODY_BYTES:
                        raise ContractError("broker_unavailable", "fleet response exceeds 64 KiB", 503)
                    raw.extend(chunk)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("fleet response must be an object")
                return result
        except ContractError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            raise ContractError("broker_unavailable", "fleet admission or release unavailable", 503) from exc
