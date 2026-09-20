"""Mandatory admission for every new decision request.

Broker loss refuses cold AND warm new work. Already-admitted in-flight
requests keep their reservation until completion. This path never uses the
legacy /admit degrade-to-grant branch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from .contract import ContractError, load_profile
from .kinds import PhysicalKind, select_implementation

def _profile_limits(profile_id: str) -> tuple[str, int]:
    name = "pane-chips-v1.json" if profile_id.startswith("pane-chips") else "pane-attention-v1.json"
    profile = load_profile(name)
    return str(profile["language"]), int(profile["max_len"])


class BrokerUnavailable(Exception):
    pass


@dataclass
class Admission:
    granted: bool
    kind: Optional[str]
    backend: Optional[str]
    device_id: Optional[str]
    warm: bool
    lease_id: str


@dataclass
class InFlight:
    request_id: str
    lease_id: str
    kind: str
    owner: str
    cancelled: bool = False
    finished: bool = False


class DecisionAdmission:
    def __init__(
        self,
        *,
        broker_admit: Callable[..., dict],
        kinds: list,
        now_ms: Callable[[], int],
    ):
        self._broker_admit = broker_admit
        self.kinds: list[PhysicalKind] = kinds
        self._now_ms = now_ms
        self._inflight: Dict[str, InFlight] = {}
        self._leases = 0

    def admit(
        self,
        *,
        profile_id: str,
        owner: str,
        request_id: str,
        backend: Optional[str] = None,
        language: Optional[str] = None,
        max_len: Optional[int] = None,
    ) -> Admission:
        prof_lang, prof_max = _profile_limits(profile_id)
        chosen = select_implementation(
            self.kinds, profile_id=profile_id,
            language=language or prof_lang,
            max_len=max_len or prof_max,
            backend=backend,
        )
        if chosen is None:
            raise ContractError("profile_unavailable", "no eligible decision implementation", 503)
        try:
            result = self._broker_admit(
                kind=chosen.kind,
                profile_id=profile_id,
                owner=owner,
                warm=chosen.resident,
                backend=chosen.backend,
            )
        except BrokerUnavailable as e:
            raise ContractError(
                "broker_unavailable",
                "broker unreachable; new decision requests are refused",
                503,
            ) from e
        except Exception as e:
            # Unlike hostd /admit, ANY planning fault is a refusal, not a grant.
            raise ContractError("broker_unavailable", f"admission failed: {e}", 503) from e
        if not result or not result.get("granted"):
            raise ContractError(
                "broker_unavailable" if result and result.get("broker") == "down" else "capacity",
                result.get("reason", "admission refused") if result else "admission refused",
                429 if result and result.get("granted") is False and "capacity" in str(result) else 503,
            )
        self._leases += 1
        lease_id = str(result.get("lease_id") or f"lease-{self._leases}")
        self._inflight[request_id] = InFlight(
            request_id=request_id, lease_id=lease_id, kind=chosen.kind, owner=owner,
        )
        return Admission(
            granted=True,
            kind=chosen.kind,
            backend=chosen.backend,
            device_id=result.get("device_id") or chosen.device_id,
            warm=bool(chosen.resident),
            lease_id=lease_id,
        )

    def finish(self, request_id: str) -> None:
        item = self._inflight.get(request_id)
        if item:
            item.finished = True

    def cancel(self, request_id: str) -> None:
        item = self._inflight.get(request_id)
        if item and not item.finished:
            item.cancelled = True

    def inflight(self, request_id: str) -> Optional[InFlight]:
        return self._inflight.get(request_id)

    def retain_until_complete(self, request_id: str) -> bool:
        item = self._inflight.get(request_id)
        return bool(item and not item.finished)
