"""HTTP routes for claims, reload status and rollout state (declarative-worker-rollout).

Kept out of http.py so the authority route table gains one hook, not a block. Roles: `admin` is the
operator; `rollout` is the reconciler's own service principal with a smaller blast radius (it may
drain/enable with an expiry, post specs and units and its observation report, and never `force`, never
clear `needs_operator`, never reload anything); `caller` may read.
"""
from __future__ import annotations

import datetime

from .model import WorkloadError, name
from . import reload_status as _reload

NOT_HANDLED = (False, None)


def _until(value):
    """Epoch seconds, or an ISO-8601 string such as 2026-10-09T06:00:00Z."""
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise WorkloadError('until must be epoch seconds or ISO-8601', 400)
    return value


def _known_workers(server):
    known = {p.worker for p in server.principals if p.role == 'worker'}
    with server.store.transaction() as db:
        known |= {r['id'] for r in db.execute('SELECT id FROM workers')}
    return known


def route(server, principal, method, parts, body):
    """(handled, answer). Unhandled routes fall through to the ordinary table."""
    if parts == ['workers'] and method == 'GET' and principal.role == 'rollout':
        from . import roster  # the reconciler reads the same roster callers and admins do
        return True, roster.build(server.store, server.principals)
    if not parts or parts[0] not in ('claims', 'reload', 'rollout'):
        return NOT_HANDLED
    operator, writer = principal.role == 'admin', principal.role in ('admin', 'rollout')
    reader = principal.role in ('admin', 'rollout', 'caller')
    if parts[0] == 'claims':
        if method == 'GET' and reader and parts == ['claims']:
            return True, server.claims.listing()
        if method == 'GET' and reader and len(parts) == 2:
            row = server.claims.get(name(parts[1], 'worker'))
            if row is None:
                raise WorkloadError('no claim row for this worker', 404)
            return True, row
        if method == 'POST' and writer and len(parts) == 3 and parts[2] in ('drain', 'enable'):
            owner = body.get('owner') or principal.id
            if body.get('force') and not operator:
                raise WorkloadError('force requires an admin principal', 403)
            known = _known_workers(server)
            if parts[2] == 'drain':
                if body.get('needs_operator') is not None and type(body['needs_operator']) is not bool:
                    raise WorkloadError('needs_operator must be boolean', 400)
                return True, server.claims.drain(
                    parts[1], owner=owner, reason=body.get('reason', ''), ttl_seconds=body.get('ttl_seconds'),
                    until=_until(body.get('until')), if_generation=body.get('if_generation'),
                    force=bool(body.get('force')), needs_operator=bool(body.get('needs_operator')), known=known)
            return True, server.claims.enable(
                parts[1], owner=owner, reason=body.get('reason', ''), if_generation=body.get('if_generation'),
                force=bool(body.get('force')), operator=operator, known=known)
        raise WorkloadError('claims routes require an admin or rollout principal', 403)
    if parts == ['reload', 'status'] and method == 'GET':
        if not writer:
            raise WorkloadError('reload status requires an admin or rollout principal', 403)
        return True, server.reload_status.snapshot()
    if parts[0] == 'rollout':
        if method == 'GET' and reader and parts == ['rollout']:
            return True, server.rollout.status()
        if method == 'POST' and writer and parts == ['rollout', 'spec']:
            return True, server.rollout.set_spec(body.get('spec'), if_generation=body.get('if_generation'),
                                                 actor=principal.id)
        if method == 'POST' and writer and parts == ['rollout', 'units']:
            return True, server.rollout.put_unit(body.get('manifest'))
        if method == 'POST' and principal.role == 'rollout' and parts == ['rollout', 'report']:
            return True, server.rollout.put_report(body.get('report'))
        raise WorkloadError('rollout routes require an admin or rollout principal', 403)
    return NOT_HANDLED
