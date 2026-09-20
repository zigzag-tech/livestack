"""Decision-request authentication.

Owner comes from the credential and scoped headers, never from a body
``account_id``. A missing realm is a refusal, not ``mesh``.
"""
from __future__ import annotations

from typing import Mapping, Optional, Tuple

from livestack_node.fleet_auth import AuthError, Principal, authenticate, bearer_token, principal_for

from .contract import ContractError


def authenticate_decision(
    principals: Mapping[str, Principal],
    *,
    authorization: Optional[str],
    realm: Optional[str],
    owner_header: Optional[str],
    body: Optional[Mapping] = None,
) -> Tuple[str, str, Principal]:
    """Return ``(owner, realm, principal)`` or raise ContractError."""
    if body and "account_id" in body:
        # Present so a forged body field cannot be mistaken for identity even
        # when headers are correct: we refuse the extra property at schema
        # time, and we refuse here if a caller bypassed schema.
        raise ContractError("invalid_input", "account_id is not an identity field", 400)
    realm_id = (realm or "").strip()
    if not realm_id:
        raise ContractError("forbidden", "X-Harmony-Realm is required; missing scope is not mesh", 403)
    try:
        owner, principal = authenticate(principals, authorization, owner_header)
    except AuthError as e:
        cause = "unauthorized" if e.status == 401 else "forbidden" if e.status == 403 else "invalid_input"
        raise ContractError(cause, e.detail, e.status) from e
    # A fixed principal's header owner must equal its identity. authenticate()
    # already enforces that. A delegating principal must name the owner.
    if principal.delegates and not (owner_header or "").strip():
        raise ContractError("invalid_input", "delegating principal must send X-Harmony-Owner", 400)
    if not principal.delegates and owner_header and owner_header.strip() != principal.owner:
        raise ContractError("forbidden", "fixed principal scope must equal X-Harmony-Owner", 403)
    return owner, realm_id, principal


def refuse_unauthenticated_legacy_admit(authorization: Optional[str]) -> None:
    """Decision traffic cannot use the unauthenticated legacy /admit path."""
    if bearer_token(authorization) is None:
        raise ContractError("unauthorized", "decision admission requires a bearer token", 401)
