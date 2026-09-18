"""Independent scenario curation with mechanism deduplication."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .contracts import ContractError


class ScenarioRegistry:
    def __init__(self) -> None:
        self._scenarios: dict[str, dict[str, Any]] = {}

    def admit(
        self,
        proposal: Any,
        *,
        author_identity: str,
        curator_identity: str,
    ) -> dict[str, Any]:
        if not author_identity or not curator_identity or author_identity == curator_identity:
            raise ContractError("scenario admission requires an independent curator")
        if not isinstance(proposal, dict):
            raise ContractError("scenario proposal must be an object")
        mechanism = proposal.get("mechanism_id")
        topology = proposal.get("topology")
        assertions = proposal.get("assertions")
        incident = proposal.get("incident_sha256")
        if not isinstance(mechanism, str) or not mechanism or not isinstance(topology, str) or not topology:
            raise ContractError("scenario mechanism and topology are required")
        if not isinstance(assertions, list) or not assertions or any(not isinstance(item, str) or not item for item in assertions):
            raise ContractError("scenario assertions must be non-empty strings")
        if not isinstance(incident, str) or not re.fullmatch(r"[0-9a-f]{64}", incident):
            raise ContractError("incident hash must be SHA-256")
        oracle = hashlib.sha256(
            json.dumps(
                {"mechanism_id": mechanism, "topology": topology, "assertions": assertions},
                separators=(",", ":"), sort_keys=True,
            ).encode()
        ).hexdigest()
        existing = self._scenarios.get(mechanism)
        if existing is None:
            existing = {
                "scenario_id": hashlib.sha256(mechanism.encode()).hexdigest()[:24],
                "mechanism_id": mechanism,
                "oracle_sha256": oracle,
                "oracle_count": 1,
                "incident_hashes": set(),
            }
            self._scenarios[mechanism] = existing
        elif existing["oracle_sha256"] != oracle:
            raise ContractError("repeated incident cannot multiply or mutate oracle authority")
        existing["incident_hashes"].add(incident)
        return {
            "scenario_id": existing["scenario_id"],
            "mechanism_id": mechanism,
            "oracle_sha256": oracle,
            "oracle_count": 1,
            "incident_count": len(existing["incident_hashes"]),
            "traffic_weight": len(existing["incident_hashes"]),
            "curator_identity": curator_identity,
        }
