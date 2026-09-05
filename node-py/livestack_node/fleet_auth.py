"""fleet_auth.py — who is asking, decided by the credential and never by the body.

The quota in `fleet_scheduler.py` is worth exactly what `Job.owner` is worth, and
until now `owner` was a string in the request body. An account raised its own
ceiling by renaming itself, and `/fleet/admit` had no authentication at all — the
broker binds `0.0.0.0`, so anything that could reach the port could spend
anyone's capacity under anyone's name.

This module is the fix, and it is a **pure function**: a token string plus a
requested owner in, a resolved identity or a refusal out. No I/O, no clock, no
framework. The ordering rules in this codebase are pure for the same reason —
the interesting behaviour is the decision, and a decision that needs a socket to
test is a decision nobody tests.

**Two kinds of principal, because two kinds of caller exist.**

* A **fixed** principal is one service with one identity: media-corpus's ingest
  is `media-corpus` and can be nothing else. Its token names its owner, and a
  body that disagrees is refused rather than silently overridden — silent
  substitution is how a caller ends up debugging the wrong account's quota.
* A **delegating** principal has authenticated somebody else and speaks for
  them: the hub, admitting work for an account that just signed in. It may name
  an owner, but only within a prefix it was granted, so the hub cannot spend
  `media-corpus`'s quota by asking for it.

The delegation prefix is the whole reason this is not just "a shared secret".
Without it, one compromised caller is every account.

**What this does NOT do**, stated so nobody assumes otherwise: it does not
authenticate the END USER. A delegating principal asserts "I checked this
account", and the fleet broker believes it. That is the correct trust boundary —
the fleet broker cannot verify a benchday login — but it means a delegating
token is as strong as the service holding it, and should be issued only to
services that authenticate their own callers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Principal:
    """Who a token says its bearer is."""

    #: The principal's own id. Appears in logs and in the decision ledger; it is
    #: NOT the owner, because a delegating principal acts for many owners.
    name: str
    #: The one owner this token may act as, or None when it delegates.
    owner: Optional[str] = None
    #: Owners this token may name, by prefix. None when it is fixed.
    delegate_prefix: Optional[str] = None

    @property
    def delegates(self) -> bool:
        return self.delegate_prefix is not None


class AuthError(Exception):
    """Refused. `status` is what the HTTP layer should return: 401 when we do
    not know who this is, 403 when we do and they may not do this."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def fingerprint(token: str) -> str:
    """A short, stable, NON-reversible handle for a token.

    Logs and error messages use this. A token is a credential; the one place it
    must never appear is the place people paste when asking for help.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def load_principals(raw: str,
                    log=lambda *_: None) -> Dict[str, Principal]:
    """Parse `LIVESTACK_FLEET_TOKENS`. Malformed entries are SKIPPED, loudly.

    Skipped rather than fatal for the same reason the quota parse is: this is
    the setting that changes whenever a caller is added or removed, so it is the
    one most likely to be mistyped, and one bad entry must not take every other
    caller's access with it. But a skipped entry is a caller that will now get
    401, so it is logged with its fingerprint — never its value.

    Shape::

        {"<token>": {"name": "media-corpus", "owner": "media-corpus"},
         "<token>": {"name": "hub", "delegate_prefix": "acct_"}}
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        log(f"[fleet-auth] LIVESTACK_FLEET_TOKENS is not a JSON object ({e}) — "
            f"NO principals loaded, so /fleet/admit will refuse every caller. "
            f"NOTE systemd strips bare double quotes; wrap the value in single "
            f"quotes.")
        return {}

    out: Dict[str, Principal] = {}
    for token, spec in parsed.items():
        fp = fingerprint(str(token))
        if not isinstance(token, str) or len(token) < 16:
            # A short token is a guessable one. Refusing it here is better than
            # accepting a credential that cannot carry the weight put on it.
            log(f"[fleet-auth] token {fp} rejected: shorter than 16 characters")
            continue
        if not isinstance(spec, dict):
            log(f"[fleet-auth] token {fp} rejected: value is not an object")
            continue
        owner = spec.get("owner")
        prefix = spec.get("delegate_prefix")
        name = str(spec.get("name") or owner or "unnamed")
        if owner and prefix:
            log(f"[fleet-auth] token {fp} ({name}) rejected: has BOTH owner and "
                f"delegate_prefix — a principal is one or the other, and "
                f"guessing which would be guessing at a security boundary")
            continue
        if not owner and not prefix:
            log(f"[fleet-auth] token {fp} ({name}) rejected: names neither an "
                f"owner nor a delegate_prefix, so it could act as anyone")
            continue
        out[token] = Principal(name=name,
                               owner=str(owner) if owner else None,
                               delegate_prefix=str(prefix) if prefix else None)
    return out


def bearer_token(header: Optional[str]) -> Optional[str]:
    """The token out of an `Authorization: Bearer <token>` header, or None."""
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def principal_for(principals: Mapping[str, Principal],
                  token: Optional[str]) -> Principal:
    """The principal this token names, or raise 401.

    Compared in CONSTANT TIME against every configured token. A plain dict
    lookup would answer faster for a wrong token than a right one, and that
    difference is a side channel that leaks the token a character at a time.
    """
    if not token:
        raise AuthError(401, "missing bearer token")
    found: Optional[Principal] = None
    for candidate, principal in principals.items():
        if hmac.compare_digest(candidate, token):
            found = principal
    if found is None:
        raise AuthError(401, "unknown bearer token")
    return found


def resolve_owner(principal: Principal, requested: Optional[str]) -> str:
    """The owner this request will be accounted to.

    The credential decides. `requested` is only ever consulted for a delegating
    principal, and even then only within its granted prefix.
    """
    asked = (requested or "").strip() or None
    if not principal.delegates:
        if asked and asked != principal.owner:
            # REFUSED, not silently overridden. A caller that believes it is
            # spending account X's quota while the broker charges account Y will
            # debug the wrong account, and the difference will not show up until
            # somebody reads the ledger months later.
            raise AuthError(
                403,
                f"token acts as '{principal.owner}' and cannot name "
                f"'{asked}'; omit `owner` or use a delegating token")
        return principal.owner  # type: ignore[return-value]
    if not asked:
        raise AuthError(
            400, f"'{principal.name}' is a delegating principal and must name "
                 f"an `owner`; it has no identity of its own to charge")
    if not asked.startswith(principal.delegate_prefix or ""):
        # The prefix is what stops one compromised delegating caller from being
        # every account. Without it a hub token could spend media-corpus's quota
        # by asking for it.
        raise AuthError(
            403,
            f"'{principal.name}' may only act for owners starting with "
            f"'{principal.delegate_prefix}'; '{asked}' is outside that")
    return asked


def authenticate(principals: Mapping[str, Principal],
                 header: Optional[str],
                 requested_owner: Optional[str]) -> Tuple[str, Principal]:
    """`(owner, principal)` for this request, or raise :class:`AuthError`.

    The single entry point, so no call site can accidentally use the body's
    owner by forgetting a step.
    """
    principal = principal_for(principals, bearer_token(header))
    return resolve_owner(principal, requested_owner), principal
