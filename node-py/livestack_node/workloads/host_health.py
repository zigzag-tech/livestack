"""Durable host health: a physical host that must take no new work until its owner clears it.

Why this is not a drain (claims.py). A drain is per WORKER, must carry an expiry of at most 24 h, and
expires back into service on the authority's tick. That is right for a roll and wrong for a host whose
RAM is under diagnosis: on 2026-11-14 node-h flipped bit 60 on non-ECC DIMMs, its sticks are being
swapped one at a time, and every swap changes the RAM the workers measure. A hold that a TTL, a
capacity change or a reboot could lift would put E2E/build work onto memory that silently corrupts it.

Semantics (the whole contract):
- Keyed by the physical `host` value of the worker principals, so it covers every worker identity on
  that machine, including ones not registered yet.
- No expiry. Nothing in the authority clears it: not `register` (any capacity, any boot), not the
  claims tick, not an authority restart. Only `release` does.
- `release` by anyone but the holding owner needs `force` (an admin), exactly like a drain held by
  another owner. The owner is whoever owns the verdict (for node-h, the memory-test agent).
- Running attempts are not touched. A hold stops NEW placements; it never kills.
- `generation` (+1 per change) lets a writer compare-and-swap, as claims do.
"""
from __future__ import annotations

from .model import WorkloadError, name

MAX_REASON = 240
HELD, ELIGIBLE = 'held', 'eligible'


def _row(row):
    return None if row is None else dict(
        host=row['host'], state=row['state'], held=row['state'] == HELD, generation=row['generation'],
        owner=row['owner'], reason=row['reason'], updated_at=row['updated_at'])


def held_hosts(db):
    """{host: row} for every held host: one query, read by placement and `claims.draining`."""
    return {r['host']: _row(r) for r in db.execute("SELECT * FROM host_health WHERE state=?", (HELD,))}


class HostHealth:
    """Host health operations over a WorkloadStore's database."""

    def __init__(self, store, ledger=None):
        self.store, self.ledger = store, ledger

    def get(self, host):
        with self.store.transaction() as db:
            return _row(db.execute('SELECT * FROM host_health WHERE host=?', (name(host, 'host'),)).fetchone())

    def listing(self):
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM host_health ORDER BY host LIMIT ?',
                              (self.store.limits.workers * 2,)).fetchall()
        return dict(hosts=[_row(r) for r in rows])

    def _set(self, host, state, *, owner, reason, if_generation, force):
        name(host, 'host'); name(owner, 'owner')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
            raise WorkloadError('host health needs a reason (at most 240 characters)', 400)
        now = self.store.clock()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM host_health WHERE host=?', (host,)).fetchone()
            have = 0 if row is None else row['generation']
            if if_generation is not None and (type(if_generation) is not int or have != if_generation):
                raise WorkloadError(f'host_health_generation_conflict: have {have}, caller expected '
                                    f'{if_generation}', 409)
            if row is not None and row['state'] == HELD and row['owner'] != owner and not force:
                raise WorkloadError(f"host_health_held_by:{row['owner']}", 409)
            db.execute('INSERT INTO host_health(host,state,generation,owner,reason,updated_at) '
                       'VALUES(?,?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET state=excluded.state,'
                       'generation=excluded.generation,owner=excluded.owner,reason=excluded.reason,'
                       'updated_at=excluded.updated_at', (host, state, have + 1, owner, reason, now))
            new = _row(db.execute('SELECT * FROM host_health WHERE host=?', (host,)).fetchone())
        if self.ledger is not None:
            self.ledger.append('host_hold' if state == HELD else 'host_release', host=host, owner=owner,
                               reason=reason, generation=new['generation'],
                               forced=bool(force and row is not None and row['state'] == HELD
                                           and row['owner'] != owner))
        return new

    def hold(self, host, *, owner, reason, if_generation=None, force=False):
        return self._set(host, HELD, owner=owner, reason=reason, if_generation=if_generation, force=force)

    def release(self, host, *, owner, reason, if_generation=None, force=False):
        return self._set(host, ELIGIBLE, owner=owner, reason=reason, if_generation=if_generation, force=force)
