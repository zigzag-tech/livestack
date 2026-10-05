"""Bounded, durable registry for independently released workload handlers."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import PurePosixPath
import re
import sqlite3
import tarfile
import time

from .handler_release import (MAX_MANIFEST_BYTES, MAX_PACKAGE_BYTES, validate_manifest)
from .model import WorkloadError, encode

MAX_HANDLER_IDS = 64
# Total catalogued releases across every handler. There is deliberately NO per-handler
# count: storage is the bound (bytes, metadata, unreferenced candidates, and this
# total, which matches a worker's 256 installed-package cap and the status listing).
MAX_TOTAL_RELEASES = 256
MAX_REGISTRY_BYTES = 16 * 1024**3
MAX_METADATA_BYTES = 4 * 1024**2
MAX_STAGED_CANDIDATES = 16
MAX_RECEIPTS = 16
MAX_GENERATIONS = 2
MAX_EVENTS = 1024
MINIMUM_RETENTION_SECONDS = 24 * 60 * 60
# Capacity-driven eviction may reclaim an unreferenced release only after this
# explicit minimum age, which a policy may set no lower than one hour. The age
# protects the window between staging a release and the first reference to it
# being recorded (a job submitted by digest, a worker inventory report); the
# complete-reference-evidence check below remains the safety net.
MINIMUM_BURST_AGE_SECONDS = 60 * 60
MAX_EVICTIONS_PER_STAGE = 32
_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class HandlerReleaseRegistry:
    """Registry transitions share the authority's SQLite transaction boundary."""

    def __init__(self, store, blobs, policy=None):
        self.store, self.blobs = store, blobs
        config = policy or {}
        if (not isinstance(config, dict) or set(config) - {'revision', 'handlers', 'retention_seconds', 'burst_min_age_seconds'} or
                not isinstance(config.get('handlers', {}), dict) or
                len(config.get('handlers', {})) > MAX_HANDLER_IDS):
            raise ValueError('invalid handler release policy')
        self.policy_revision = str(config.get('revision', '1'))[:64]
        retention = config.get('retention_seconds')
        if retention is not None and (type(retention) is not int or
                not MINIMUM_RETENTION_SECONDS <= retention <= 10*365*24*60*60):
            raise ValueError('handler release retention must be unset or at least 24 hours')
        self.retention_seconds = retention
        burst = config.get('burst_min_age_seconds')
        if burst is not None and (type(burst) is not int or burst < MINIMUM_BURST_AGE_SECONDS or
                burst > 10*365*24*60*60 or (retention is not None and burst > retention)):
            raise ValueError('handler release burst eviction age must be unset or at least one hour '
                             'and no longer than the retention window')
        # Unset means capacity-driven eviction is OFF (fail closed): a full registry then refuses by name.
        self.burst_min_age_seconds = burst
        self.policy = config.get('handlers', {})
        for handler, value in self.policy.items():
            if (handler not in store.handlers or not isinstance(value, dict) or
                    set(value) - {'runtime_ids', 'backends', 'enabled'} or
                    not isinstance(value.get('runtime_ids', []), list) or
                    len(value.get('runtime_ids', [])) > 16 or
                    any(not isinstance(runtime, str) or not runtime or len(runtime) > 64
                        for runtime in value.get('runtime_ids', [])) or
                    not isinstance(value.get('backends', []), list) or not value.get('backends') or
                    len(value.get('backends', [])) > 3 or
                    any(backend not in ('native', 'rootless-docker', 'rootless-docker-native')
                        for backend in value.get('backends', [])) or
                    type(value.get('enabled', True)) is not bool):
                raise ValueError('handler release policy may only narrow installed handlers and runtimes')
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS handler_releases(
                  handler_id TEXT NOT NULL, release_digest TEXT NOT NULL, archive_digest TEXT NOT NULL,
                  archive_bytes INTEGER NOT NULL, manifest TEXT NOT NULL, actor TEXT NOT NULL,
                  created REAL NOT NULL, PRIMARY KEY(handler_id,release_digest), UNIQUE(release_digest));
                CREATE TABLE IF NOT EXISTS handler_registry_generations(
                  generation INTEGER PRIMARY KEY, defaults TEXT NOT NULL, actor TEXT NOT NULL,
                  request_id TEXT NOT NULL UNIQUE, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS handler_registry_state(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS handler_activation_receipts(
                  request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, expected_generation INTEGER NOT NULL,
                  generation INTEGER NOT NULL, handler_id TEXT NOT NULL, previous_digest TEXT,
                  release_digest TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS handler_release_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
                  handler_id TEXT NOT NULL, release_digest TEXT, archive_digest TEXT,
                  bytes INTEGER NOT NULL, outcome TEXT NOT NULL, policy_revision TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS handler_release_events_age ON handler_release_events(id);
            ''')
            if not db.execute('SELECT 1 FROM handler_registry_state WHERE singleton=1').fetchone():
                db.execute('INSERT INTO handler_registry_generations VALUES(0,?,?,?,?)',
                           ('{}', 'system', 'initial', store.clock()))
                db.execute('INSERT INTO handler_registry_state VALUES(1,0)')

    def _event(self, db, actor, handler, release, archive, size, outcome):
        db.execute('INSERT INTO handler_release_events(at,actor,handler_id,release_digest,archive_digest,bytes,'
                   'outcome,policy_revision) VALUES(?,?,?,?,?,?,?,?)',
                   (self.store.clock(), actor[:128], handler[:128], release, archive, size, outcome,
                   self.policy_revision))
        db.execute('DELETE FROM handler_release_events WHERE id IN '
                   '(SELECT id FROM handler_release_events ORDER BY id DESC LIMIT -1 OFFSET ?)', (MAX_EVENTS,))

    def record_refusal(self, actor, request, outcome):
        manifest = request.get('manifest') if isinstance(request, dict) else None
        handler = manifest.get('handler_id') if isinstance(manifest, dict) else None
        handler = handler if isinstance(handler, str) and handler else '*'
        release = request.get('release_digest') if isinstance(request, dict) else None
        archive = request.get('archive_digest') if isinstance(request, dict) else None
        release = release if isinstance(release, str) and _DIGEST.fullmatch(release) else None
        archive = archive if isinstance(archive, str) and _DIGEST.fullmatch(archive) else None
        size = request.get('archive_bytes', 0) if isinstance(request, dict) else 0
        size = size if type(size) is int and 0 <= size <= MAX_PACKAGE_BYTES else 0
        if isinstance(request, dict) and isinstance(request.get('handler_id'), str):
            handler = request['handler_id'][:128]
        with self.store.transaction() as db:
            self._event(db, actor, handler, release, archive, size, ('refused_'+str(outcome))[:96])

    @staticmethod
    def _verify_archive(stream, manifest, archive_digest, archive_bytes):
        if (not isinstance(archive_digest, str) or not _DIGEST.fullmatch(archive_digest) or
                type(archive_bytes) is not int or not 1 <= archive_bytes <= MAX_PACKAGE_BYTES):
            raise WorkloadError('handler_archive_identity_invalid', 400)
        stream.seek(0)
        hasher = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            if size > MAX_PACKAGE_BYTES:
                raise WorkloadError('handler_package_bytes_exceeded', 413)
            hasher.update(chunk)
        if size != archive_bytes or hasher.hexdigest() != archive_digest:
            raise WorkloadError('handler_archive_digest_mismatch', 409)
        canonical_size = 1024 + sum(512 + ((item['size'] + 511)//512)*512 for item in manifest['files'])
        if size < canonical_size or size % 512:
            raise WorkloadError('handler_archive_noncanonical_size', 400)
        stream.seek(size-1024)
        if stream.read(1024) != bytes(1024):
            raise WorkloadError('handler_archive_noncanonical_trailer', 400)
        stream.seek(0)
        expected = {item['path']: item for item in manifest['files']}
        seen, expanded = set(), 0
        try:
            with tarfile.open(fileobj=stream, mode='r:') as archive:
                for member in archive:
                    name = member.name
                    path = PurePosixPath(name)
                    if (not member.isfile() or name in seen or path.is_absolute() or
                            '\\' in name or str(path) != name or
                            any(part in ('', '.', '..') for part in path.parts)):
                        raise WorkloadError('handler_archive_member_refused', 400)
                    item = expected.get(name)
                    if item is None or (member.mode & 0o7777) != item['mode'] or member.size != item['size']:
                        raise WorkloadError('handler_archive_inventory_mismatch', 409)
                    seen.add(name)
                    expanded += member.size
                    source = archive.extractfile(member)
                    if source is None:
                        raise WorkloadError('handler_archive_member_unreadable', 400)
                    digest = hashlib.sha256()
                    count = 0
                    with closing(source):
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            count += len(chunk)
                            digest.update(chunk)
                    if count != item['size'] or digest.hexdigest() != item['sha256']:
                        raise WorkloadError('handler_archive_file_digest_mismatch', 409)
        except (tarfile.TarError, OSError) as error:
            raise WorkloadError('handler_archive_invalid', 400) from error
        if seen != set(expected) or expanded > MAX_PACKAGE_BYTES:
            raise WorkloadError('handler_archive_inventory_mismatch', 409)

    # One reference-evidence query serves both the ordinary retention sweep and the
    # capacity-driven eviction, so the two can never disagree about what is referenced.
    _UNREFERENCED_SQL = ("WITH referenced(digest) AS MATERIALIZED ("
        "SELECT value FROM handler_registry_generations g,json_each(g.defaults) "
        "UNION SELECT r.previous_digest FROM handler_activation_receipts r WHERE r.previous_digest IS NOT NULL "
        "AND r.request_id=(SELECT newest.request_id FROM handler_activation_receipts newest "
        "WHERE newest.handler_id=r.handler_id ORDER BY newest.created DESC LIMIT 1) "
        "UNION SELECT effective.value FROM workers w,json_each(w.report,'$.handler_inventory.defaults') effective "
        "WHERE w.seen>? "
        "UNION SELECT json_extract(spec,'$.handler_release.release_digest') FROM jobs "
        "WHERE state IN ('queued','running') "
        "UNION SELECT json_extract(handler_release,'$.release_digest') FROM attempts "
        "WHERE state IN ('running','cleanup')) "
        "SELECT r.handler_id,r.release_digest,r.archive_digest,r.archive_bytes,"
        "length(CAST(r.manifest AS BLOB)) AS manifest_bytes "
        "FROM handler_releases r WHERE r.created<=? AND r.release_digest NOT IN "
        "(SELECT digest FROM referenced WHERE digest IS NOT NULL) "
        "ORDER BY r.created,r.release_digest LIMIT ?")

    def _unreferenced(self, db, now, cutoff, limit):
        """Oldest-first releases created at or before `cutoff` that nothing references."""
        return db.execute(self._UNREFERENCED_SQL, (now-self.store.limits.fresh_seconds, cutoff, limit)).fetchall()

    def _make_room(self, db, actor, now, reason, deficits):
        """Evict just enough aged, unreferenced releases to cover `deficits`.

        deficits = (bytes, manifest_bytes, releases, candidates) still to free. All-or-nothing:
        nothing is deleted unless the whole deficit can be covered, so a refusal never costs a
        release. Never evicts a default, rollback target, queued/running job or attempt, fresh
        worker inventory, or anything younger than the policy's burst age. Unknown reference
        evidence evicts nothing. Bounded: one evidence query and at most MAX_EVICTIONS_PER_STAGE
        deletes, independent of how many releases exist. Returns the evicted rows ([] if none).
        """
        if self.burst_min_age_seconds is None:
            return []
        try:
            rows = self._unreferenced(db, now, now-self.burst_min_age_seconds, MAX_EVICTIONS_PER_STAGE)
        except sqlite3.Error:
            self._event(db, actor, '*', None, None, 0, 'handler_release_eviction_evidence_unavailable')
            return []
        need = list(deficits)
        chosen = []
        for row in rows:
            if all(value <= 0 for value in need):
                break
            chosen.append(row)
            need[0] -= row['archive_bytes']
            need[1] -= row['manifest_bytes']
            need[2] -= 1
            need[3] -= 1
        if any(value > 0 for value in need):
            return []
        for row in chosen:
            db.execute('DELETE FROM handler_releases WHERE handler_id=? AND release_digest=? AND created<=?',
                       (row['handler_id'], row['release_digest'], now-self.burst_min_age_seconds))
            self._event(db, actor, row['handler_id'], row['release_digest'], row['archive_digest'],
                        row['archive_bytes'], 'evicted_for_'+reason)
        return chosen

    def stage(self, actor, request):
        required = {'manifest', 'release_digest', 'archive_digest', 'archive_bytes'}
        if not isinstance(request, dict) or set(request) != required:
            raise WorkloadError('handler_stage_request_invalid', 400)
        checked = validate_manifest(request['manifest'], request['release_digest'])
        manifest = checked['manifest']
        handler, digest = manifest['handler_id'], checked['release_digest']
        policy = self.policy.get(handler)
        if (policy is None or not policy.get('enabled', True) or handler not in self.store.handlers or
                manifest['runtime_id'] not in policy.get('runtime_ids', []) or
                manifest['backend'] not in policy.get('backends', [])):
            with self.store.transaction() as db:
                self._event(db, actor, handler, digest, request.get('archive_digest'),
                            request.get('archive_bytes', 0) if type(request.get('archive_bytes', 0)) is int else 0,
                            'refused_policy')
            raise WorkloadError('handler_release_policy_refused', 403)
        try:
            with self.blobs.open(actor, request['archive_digest']) as (stream, actual_size):
                if actual_size != request['archive_bytes']:
                    raise WorkloadError('handler_archive_size_mismatch', 409)
                self._verify_archive(stream, manifest, request['archive_digest'], actual_size)
        except WorkloadError as error:
            with self.store.transaction() as db:
                self._event(db, actor, handler, digest, request.get('archive_digest'),
                            request.get('archive_bytes', 0) if type(request.get('archive_bytes', 0)) is int else 0,
                            str(error)[:96])
            raise
        self.collect(actor=actor)
        now = self.store.clock()
        refusal = None
        evicted = []
        with self.store.transaction() as db:
            existing = db.execute('SELECT * FROM handler_releases WHERE handler_id=? AND release_digest=?',
                                  (handler, digest)).fetchone()
            if existing:
                if existing['archive_digest'] != request['archive_digest']:
                    raise WorkloadError('handler_release_digest_collision', 409)
                self._event(db, actor, handler, digest, request['archive_digest'], actual_size, 'stage_idempotent')
                return {'handler_id': handler, 'release_digest': digest, 'state': 'staged', 'idempotent': True}
            count, used = db.execute('SELECT count(*),coalesce(sum(archive_bytes),0) FROM handler_releases').fetchone()
            candidates = db.execute('SELECT count(*) FROM handler_releases r WHERE NOT EXISTS '
                '(SELECT 1 FROM handler_registry_generations g, json_each(g.defaults) d '
                'WHERE d.value=r.release_digest)', ()).fetchone()[0]
            metadata = db.execute('SELECT coalesce(sum(length(CAST(manifest AS BLOB))),0) '
                                  'FROM handler_releases').fetchone()[0]
            new_meta = len(checked['canonical_bytes'])
            known = {r[0] for r in db.execute('SELECT DISTINCT handler_id FROM handler_releases')}
            # Storage bounds only: no per-handler release count. A bound that is exceeded is first
            # relieved by evicting aged unreferenced releases; it refuses by name only when it cannot.
            deficits = {'byte_capacity': used + actual_size - MAX_REGISTRY_BYTES,
                        'metadata_capacity': metadata + new_meta - MAX_METADATA_BYTES,
                        'release_capacity': count + 1 - MAX_TOTAL_RELEASES,
                        'staging_capacity': candidates + 1 - MAX_STAGED_CANDIDATES}
            names = {'byte_capacity': 'handler_registry_byte_capacity',
                     'metadata_capacity': 'handler_registry_metadata_capacity',
                     'release_capacity': 'handler_registry_release_capacity',
                     'staging_capacity': 'handler_registry_staging_capacity'}
            if len(known) >= MAX_HANDLER_IDS and handler not in known:
                refusal = 'handler_registry_id_capacity'
            elif (actual_size > MAX_REGISTRY_BYTES or new_meta > MAX_METADATA_BYTES):
                refusal = names['byte_capacity' if actual_size > MAX_REGISTRY_BYTES else 'metadata_capacity']
            else:
                over = [key for key in names if deficits[key] > 0]
                refusal = None
                if over:
                    evicted = self._make_room(db, actor, now, over[0],
                        (deficits['byte_capacity'], deficits['metadata_capacity'],
                         deficits['release_capacity'], deficits['staging_capacity']))
                    if not evicted:
                        refusal = names[over[0]]
            if refusal:
                self._event(db, actor, handler, digest, request['archive_digest'], actual_size, refusal)
            else:
                db.execute('INSERT INTO handler_releases VALUES(?,?,?,?,?,?,?)',
                           (handler, digest, request['archive_digest'], actual_size,
                            checked['canonical_bytes'].decode('utf-8'), actor[:128], now))
                self._event(db, actor, handler, digest, request['archive_digest'], actual_size, 'staged')
        if refusal:
            raise WorkloadError(refusal, 429)
        result = {'handler_id': handler, 'release_digest': digest, 'state': 'staged', 'idempotent': False}
        if evicted:
            result['evicted'] = len(evicted)
        return result

    def _current(self, db):
        row = db.execute('SELECT generation FROM handler_registry_state WHERE singleton=1').fetchone()
        return row['generation']

    def _defaults(self, db, generation=None):
        generation = self._current(db) if generation is None else generation
        row = db.execute('SELECT defaults FROM handler_registry_generations WHERE generation=?', (generation,)).fetchone()
        if row is None:
            raise WorkloadError('handler_registry_generation_missing', 503)
        return json.loads(row['defaults'])

    def activate(self, actor, request, *, rollback=False):
        required = {'request_id', 'expected_generation', 'handler_id', 'release_digest'}
        if not isinstance(request, dict) or set(request) != required:
            raise WorkloadError('handler_activation_request_invalid', 400)
        request_id, handler, digest, expected = (request[k] for k in
            ('request_id', 'handler_id', 'release_digest', 'expected_generation'))
        if (not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id) or
                not isinstance(handler, str) or len(handler) > 128 or
                not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or
                type(expected) is not int or expected < 0):
            raise WorkloadError('handler_activation_request_invalid', 400)
        now = self.store.clock()
        with self.store.transaction() as db:
            prior = db.execute('SELECT * FROM handler_activation_receipts WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                if (prior['actor'] == actor and prior['handler_id'] == handler and
                        prior['release_digest'] == digest and prior['expected_generation'] == expected):
                    return self._receipt(prior, idempotent=True)
                self._event(db, actor, handler, digest, None, 0, 'handler_activation_request_id_conflict')
                request_conflict = True
                conflict = None
            else:
                request_conflict = False
                observed = self._current(db)
            if not request_conflict and observed != expected:
                self._event(db, actor, handler, digest, None, 0,
                            f'handler_registry_generation_conflict:{expected}:{observed}')
                conflict = observed
            elif not request_conflict:
                conflict = None
        if request_conflict:
            raise WorkloadError('handler_activation_request_id_conflict', 409)
        if conflict is not None:
            raise WorkloadError(f'handler_registry_generation_conflict: expected {expected}, current {conflict}', 409)
        with self.store.transaction() as db:
            prior = db.execute('SELECT * FROM handler_activation_receipts WHERE request_id=?', (request_id,)).fetchone()
            if prior:
                if (prior['actor'] != actor or prior['handler_id'] != handler or prior['release_digest'] != digest or
                        prior['expected_generation'] != expected):
                    raise WorkloadError('handler_activation_request_id_conflict', 409)
                return self._receipt(prior, idempotent=True)
            current = self._current(db)
            if current != expected:
                raise WorkloadError(f'handler_registry_generation_conflict: expected {expected}, current {current}', 409)
            candidate = db.execute('SELECT * FROM handler_releases WHERE handler_id=? AND release_digest=?',
                                   (handler, digest)).fetchone()
            if candidate is None:
                raise WorkloadError('handler_release_not_staged', 404)
            defaults = self._defaults(db, current)
            previous = defaults.get(handler)
            if previous == digest:
                # A distinct request for an already effective digest is a durable no-op receipt.
                generation = current
            else:
                defaults[handler] = digest
                generation = current + 1
                encoded = encode(defaults, MAX_METADATA_BYTES)
                db.execute('INSERT INTO handler_registry_generations VALUES(?,?,?,?,?)',
                           (generation, encoded, actor[:128], request_id, now))
                db.execute('UPDATE handler_registry_state SET generation=? WHERE singleton=1 AND generation=?',
                           (generation, current))
                if db.execute('SELECT changes()').fetchone()[0] != 1:
                    raise WorkloadError('handler_registry_generation_conflict', 409)
                db.execute('DELETE FROM handler_registry_generations WHERE generation NOT IN '
                           '(SELECT generation FROM handler_registry_generations ORDER BY generation DESC LIMIT ?)',
                           (MAX_GENERATIONS,))
            db.execute('INSERT INTO handler_activation_receipts VALUES(?,?,?,?,?,?,?,?)',
                       (request_id, actor[:128], expected, generation, handler, previous, digest, now))
            db.execute('DELETE FROM handler_activation_receipts WHERE request_id IN '
                       '(SELECT request_id FROM handler_activation_receipts ORDER BY created DESC LIMIT -1 OFFSET ?)',
                       (MAX_RECEIPTS,))
            self._event(db, actor, handler, digest, candidate['archive_digest'], candidate['archive_bytes'],
                        'rolled_back' if rollback else 'activated')
            return {'request_id': request_id, 'actor': actor, 'expected_generation': expected,
                    'generation': generation, 'handler_id': handler, 'previous_digest': previous,
                    'release_digest': digest, 'idempotent': False}

    @staticmethod
    def _receipt(row, idempotent):
        return {'request_id': row['request_id'], 'actor': row['actor'],
                'expected_generation': row['expected_generation'], 'generation': row['generation'],
                'handler_id': row['handler_id'], 'previous_digest': row['previous_digest'],
                'release_digest': row['release_digest'], 'idempotent': idempotent}

    def desired(self, handlers):
        with self.store.transaction() as db:
            generation = self._current(db)
            defaults = self._defaults(db, generation)
            supported = sorted(set(handlers))
            selected = {handler: digest for handler, digest in defaults.items() if handler in supported}
            digests = set(selected.values())
            if supported:
                placeholders = ','.join('?' for _ in supported)
                for row in db.execute("SELECT DISTINCT json_extract(spec,'$.handler_release.release_digest') AS digest "
                    "FROM jobs WHERE state IN ('queued','running') "
                    f"AND json_extract(spec,'$.handler') IN ({placeholders}) "
                    "AND json_extract(spec,'$.handler_release.release_digest') IS NOT NULL LIMIT 256", supported):
                    if row['digest']:
                        digests.add(row['digest'])
            items = []
            if digests:
                values = sorted(digests)
                placeholders = ','.join('?' for _ in values)
                rows = db.execute('SELECT handler_id,release_digest,archive_digest,archive_bytes,manifest '
                                   f'FROM handler_releases WHERE release_digest IN ({placeholders})', values).fetchall()
                by_digest = {row['release_digest']: row for row in rows}
                for digest in values:
                    row = by_digest.get(digest)
                    if row is None:
                        raise WorkloadError('handler_registry_release_missing', 503)
                    items.append(dict(row, manifest=json.loads(row['manifest'])))
            response = {'generation': generation, 'defaults': selected, 'releases': items,
                        'retention_seconds': self.retention_seconds, 'references_complete': True}
            encode(response, MAX_METADATA_BYTES + 64*1024)
            return response

    def collect(self, *, actor='system'):
        """Delete only aged releases with complete, bounded reference evidence."""
        if self.retention_seconds is None:
            with self.store.transaction() as db:
                self._event(db, actor, '*', None, None, 0, 'handler_release_retention_unconfigured')
            return {'outcome': 'refused', 'reason': 'handler_release_retention_unconfigured', 'deleted': 0,
                    'bytes_reclaimed': 0}
        now = self.store.clock()
        try:
            with self.store.transaction() as db:
                rows = self._unreferenced(db, now, now-self.retention_seconds, 32)
                deleted, reclaimed = 0, 0
                for row in rows:
                    cursor = db.execute('DELETE FROM handler_releases WHERE handler_id=? AND release_digest=? '
                                        'AND created<=?', (row['handler_id'], row['release_digest'],
                                                          now-self.retention_seconds))
                    if cursor.rowcount:
                        deleted += 1
                        reclaimed += row['archive_bytes']
                        self._event(db, actor, row['handler_id'], row['release_digest'], row['archive_digest'],
                                    row['archive_bytes'], 'garbage_collected')
            return {'outcome': 'complete', 'reason': None, 'deleted': deleted,
                    'bytes_reclaimed': reclaimed, 'reference_evidence': 'complete'}
        except Exception as error:
            return {'outcome': 'refused', 'reason': 'handler_release_reference_evidence_unavailable',
                    'error': type(error).__name__[:64], 'deleted': 0, 'bytes_reclaimed': 0,
                    'reference_evidence': 'unavailable'}

    def status(self):
        with self.store.transaction() as db:
            generation = self._current(db)
            defaults = self._defaults(db, generation)
            workers = []
            for worker in db.execute('SELECT id,boot,seen,ready,report FROM workers ORDER BY id LIMIT 128'):
                report = json.loads(worker['report'])
                workers.append(dict(worker=worker['id'], boot=worker['boot'], seen=worker['seen'],
                    ready=bool(worker['ready']), effective_generation=(report.get('handler_inventory') or {}).get('generation'),
                    inventory=(report.get('handler_inventory') or {}).get('releases', []),
                    activation_failures=report.get('handler_activation_failures', []),
                    gc_receipts=report.get('handler_gc_receipts', [])))
            result = {'generation': generation, 'defaults': defaults, 'workers': workers,
                'policy': {'revision': self.policy_revision, 'retention_seconds': self.retention_seconds,
                    'burst_min_age_seconds': self.burst_min_age_seconds,
                    'limits': {'handler_ids': MAX_HANDLER_IDS, 'releases_total': MAX_TOTAL_RELEASES,
                               'archive_bytes': MAX_REGISTRY_BYTES, 'manifest_bytes': MAX_METADATA_BYTES,
                               'unreferenced_candidates': MAX_STAGED_CANDIDATES,
                               'releases_per_handler': None}},
                'releases': [dict(handler_id=r['handler_id'], release_digest=r['release_digest'],
                    archive_bytes=r['archive_bytes'], is_default=defaults.get(r['handler_id']) == r['release_digest'],
                    created=r['created']) for r in db.execute('SELECT * FROM handler_releases ORDER BY handler_id,created DESC LIMIT 256')],
                'recent_receipts': [dict(r) for r in db.execute(
                    'SELECT request_id,actor,expected_generation,generation,handler_id,previous_digest,release_digest,created '
                    'FROM handler_activation_receipts ORDER BY created DESC LIMIT ?', (MAX_RECEIPTS,))],
                'events': [dict(r) for r in db.execute('SELECT at,actor,handler_id,release_digest,archive_digest,bytes,outcome,policy_revision '
                    'FROM handler_release_events ORDER BY id DESC LIMIT 64')]}
            return json.loads(encode(result, MAX_METADATA_BYTES))

    def is_required_archive(self, digest):
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            return False
        with closing(self.store.connect()) as db:
            return bool(db.execute('SELECT 1 FROM handler_releases r, handler_registry_state s, '
                'handler_registry_generations g, json_each(g.defaults) d '
                'WHERE s.singleton=1 AND g.generation=s.generation AND r.release_digest=d.value '
                'AND r.archive_digest=? UNION SELECT 1 FROM handler_releases r JOIN jobs j '
                "ON r.release_digest=json_extract(j.spec,'$.handler_release.release_digest') "
                "WHERE r.archive_digest=? AND j.state IN ('queued','running') LIMIT 1",
                (digest, digest)).fetchone())

    def release_descriptor(self, digest):
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise WorkloadError('handler_release_digest_invalid', 400)
        with closing(self.store.connect()) as db:
            row = db.execute('SELECT handler_id,release_digest,manifest FROM handler_releases WHERE release_digest=?',
                              (digest,)).fetchone()
            if row is None:
                raise WorkloadError('handler_release_not_found', 404)
            return dict(handler_id=row['handler_id'], release_digest=row['release_digest'],
                        manifest=json.loads(row['manifest']))
