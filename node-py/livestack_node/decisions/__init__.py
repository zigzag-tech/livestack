"""Typed-decision Harmony service (Benchday Laya contract).

The placement-ledger schema at ``livestack_node/decision.schema.json`` is a
different artifact. This package is the request/result API for
``POST /v1/decisions``.
"""

from .contract import (
    CONTRACT_REVISION,
    SCHEMA_VERSION,
    PACKING_VERSION,
    validate_request,
    validate_result,
    validate_error,
    validate_slate,
    ContractError,
)

__all__ = [
    "CONTRACT_REVISION",
    "SCHEMA_VERSION",
    "PACKING_VERSION",
    "validate_request",
    "validate_result",
    "validate_error",
    "validate_slate",
    "ContractError",
]
