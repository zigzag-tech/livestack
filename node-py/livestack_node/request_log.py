"""request_log.py — one journal line per mutating request and per auth refusal.

WHO touched the fleet, next to WHAT: source address, method, path, status,
and — when a credential was presented and parsed — the principal name. The
caller inventory behind the token rollout (task R.1) was reconstructed from
service journals, and it turned out access lines do not reach the journal on
every service — so the R.3 gate ("zero 401s from an address not in the
inventory over 24 h") was unverifiable. This is the fix: the audit line is
written by the app itself, through the same stdout facility every other
[harmony]/[livestack] line uses, so it lands wherever the unit sends stdout —
and the unit is configured to send it to the journal.

Scope, deliberately narrow:

* MUTATING requests (anything not GET/HEAD) are logged at INFO, whatever
  their status — an admission, a warm, a refused eviction and a malformed
  one are all facts a retrospective needs.
* Auth refusals (401/403, wherever they were raised — dependency or
  handler) are logged even on read endpoints, because a probe of a write
  endpoint's guard IS a mutating intent.
* READS ARE NOT LOGGED. The gate needs mutating calls and refusals; a
  per-read emitter is how a 92,089-line log happened once already (the
  membership lesson in hostbroker), and `/fleet` is a poll surface.

What is never logged: bodies (they carry prompts and transcripts) and
tokens. A principal is a NAME; a rejected token is represented by its
8-char fingerprint — the handle logs are allowed to show, never the
credential itself (see fleet_auth.fingerprint for why).
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

# Anything that changes state. GET/HEAD are reads; they stay silent (see the
# module docstring — the gate needs writes and refusals, not a poll log).
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
REFUSAL_STATUSES = (401, 403)


def principal_label(authorization: Optional[str],
                    principals: Optional[Mapping]) -> Optional[str]:
    """The audit-safe handle for a credential: the principal's name when the
    token resolves, `unknown(<fingerprint>)` when a token was presented but
    does not match anything (the refusal case — an unknown caller is exactly
    what the inventory gate counts), None when no credential was presented.

    Never the token. A credential's only permitted appearance in a log is its
    non-reversible fingerprint."""
    if not authorization:
        return None
    from .fleet_auth import AuthError, bearer_token, fingerprint, principal_for
    token = bearer_token(authorization)
    if not token:
        return None
    try:
        return principal_for(principals or {}, token).name
    except AuthError:
        return f"unknown({fingerprint(token)})"


class AuditMiddleware:
    """Pure-ASGI middleware: one `[audit]` line per mutating request and per
    401/403, after the response (so the status is the real one). Exceptions
    are re-raised unchanged — the line is written with status 500 first,
    because a crashed admission is still an admission somebody attempted."""

    def __init__(self, app, principal_for: Callable[[dict], Optional[str]]):
        self.app = app
        self.principal_for = principal_for

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        status = None

        async def _send(message):
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException:
            if status is None:
                status = 500
                self._write(scope, status)
            raise
        if status is None:
            status = 500
        method = scope.get("method", "")
        if method in MUTATING or status in REFUSAL_STATUSES:
            self._write(scope, status)

    def _write(self, scope, status) -> None:
        client = scope.get("client") or (None, None)
        # ASGI header names and values arrive as byte strings.
        headers = {(k.decode("latin-1").lower() if isinstance(k, bytes) else str(k).lower()):
                   (v.decode("latin-1") if isinstance(v, bytes) else str(v))
                   for k, v in (scope.get("headers") or [])}
        principal = self.principal_for(headers)
        bits = [
            f"src={client[0] or '?'}",
            f"method={scope.get('method', '?')}",
            f"path={scope.get('path', '?')}",
            f"status={status}",
            f"principal={principal or '-'}",
        ]
        print("[audit] " + " ".join(bits), flush=True)


def attach(app, principal_for: Callable[[dict], Optional[str]]) -> None:
    """Wire the auditor into a FastAPI app. The principal resolver takes the
    ASGI scope's headers (lower-cased names) so each app supplies its own
    principal table: hostd resolves against `fleet_principals`, a node
    facade against its node token table."""
    app.add_middleware(AuditMiddleware, principal_for=principal_for)
