"""deployment-unit.v1: the three parts of a fix travel as one immutable, content-addressed thing.

openspec/changes/declarative-worker-rollout, D4. The overnight incidents of 2026-10-07/08 were each
a part (worker release, handler bundle, root-owned verifier copy) rolled apart from the others. A unit
names all three, plus the runtime capture size against its cap, and has the id `unit-<sha8>` of its
canonical bytes. Nothing in it carries key material (the static refusal below).

Workers report the parts they actually run under the report key `unit` (`report_part`); the roster
compares those with the desired unit (`unit_state`). Report-only in this round: a mismatch is shown, no
placement is withdrawn.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .model import WorkloadError

VERSION = 'deployment-unit.v1'
PARTS = ('release', 'handlers', 'verifier')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_COMMIT = re.compile(r'^[0-9a-f]{7,40}$')
_RELEASE = re.compile(r'^livestack-[0-9a-f]{8}$')
_KEYS = {'version', 'release', 'handlers', 'verifier', 'capture', 'min_authority', 'built_from'}
_CREDENTIAL = re.compile(r'(token|secret|passw|private|credential|api[_-]?key|keystore|signing)', re.I)
_PEM = re.compile(r'-----BEGIN [A-Z ]*(PRIVATE|KEY)')
MAX_UNIT_BYTES = 64 * 1024


def _fail(reason):
    raise WorkloadError(reason, 400)


def _no_credentials(value, path='unit'):
    """No key or string value that looks like key material, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _CREDENTIAL.search(str(key)):
                _fail(f'unit_credential_like_field:{path}.{key}')
            _no_credentials(item, f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_credentials(item, f'{path}[{index}]')
    elif isinstance(value, str) and _PEM.search(value):
        _fail(f'unit_credential_like_value:{path}')


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def validate(manifest):
    """Raise WorkloadError(400, name) or return the manifest. Closed keys, bounded, no credentials."""
    if not isinstance(manifest, dict) or set(manifest) != _KEYS:
        _fail('unit_unknown_or_missing_fields')
    _no_credentials(manifest)
    if manifest['version'] != VERSION:
        _fail('unit_version_unsupported')
    release = manifest['release']
    if (not isinstance(release, dict) or set(release) != {'name', 'content_hash'} or
            not isinstance(release['name'], str) or not _RELEASE.fullmatch(release['name']) or
            not isinstance(release['content_hash'], str) or not _HEX64.fullmatch(release['content_hash'])):
        _fail('unit_release_invalid')
    handlers = manifest['handlers']
    if (not isinstance(handlers, list) or len(handlers) > 256 or handlers != sorted(set(handlers)) or
            any(not isinstance(h, str) or not _HEX64.fullmatch(h) for h in handlers)):
        _fail('unit_handlers_invalid')
    verifier = manifest['verifier']
    if verifier is not None and (not isinstance(verifier, str) or not _HEX64.fullmatch(verifier)):
        _fail('unit_verifier_invalid')
    capture = manifest['capture']
    if (not isinstance(capture, dict) or set(capture) != {'size_bytes', 'cap_bytes'} or
            any(type(capture[k]) is not int or capture[k] < 0 for k in capture) or capture['cap_bytes'] == 0):
        _fail('unit_capture_invalid')
    if capture['size_bytes'] > capture['cap_bytes']:
        _fail(f"unit_capture_over_cap:{capture['size_bytes']}>{capture['cap_bytes']}")
    if not isinstance(manifest['min_authority'], str) or not 1 <= len(manifest['min_authority']) <= 64:
        _fail('unit_min_authority_invalid')
    built = manifest['built_from']
    if (not isinstance(built, list) or not 1 <= len(built) <= 8 or built != sorted(set(built)) or
            any(not isinstance(c, str) or not _COMMIT.fullmatch(c) for c in built)):
        _fail('unit_built_from_invalid')
    if len(canonical(manifest)) > MAX_UNIT_BYTES:
        _fail('unit_too_large')
    return manifest


def unit_id(manifest):
    return 'unit-' + hashlib.sha256(canonical(validate(manifest))).hexdigest()[:8]


def build(*, release_dir, handlers_root, digests, verifier_dir=None, capture_size, capture_cap,
          min_authority, built_from, probe_context=None):
    """Assemble a unit from what is on disk, or fail naming the missing/broken part. Nothing is
    guessed: a missing release, an uninstalled handler, a verifier path that does not exist, a capture
    over its cap, or a handler that fails the offline import/integrity probes all refuse the build."""
    from . import smoke
    release_dir = Path(release_dir)
    meta_path = release_dir / 'node-py' / 'RELEASE.json'
    try:
        meta = json.loads(meta_path.read_text())
        commit, content_hash = meta['commit'], meta['content_hash']
    except (OSError, ValueError, KeyError) as error:
        raise WorkloadError(f'unit_release_missing:{meta_path}:{type(error).__name__}', 400)
    if not isinstance(commit, str):
        _fail('unit_release_invalid')
    name = f'livestack-{commit[:8]}'
    if verifier_dir is not None:
        if not Path(verifier_dir).is_dir():
            _fail(f'unit_verifier_missing:{verifier_dir}')
        verifier = smoke.tree_digest(verifier_dir)
    else:
        verifier = None
    ctx = dict(probe_context or {}, handlers_root=str(handlers_root), digests=sorted(set(digests)),
               size_bytes=capture_size, cap_bytes=capture_cap)
    checks = [smoke.handler_integrity(ctx), smoke.handler_import(ctx), smoke.capture_size(ctx)]
    for result in checks:
        if result['status'] != smoke.PASS:
            _fail(result.get('failure') or f"unit_build_check_not_passed:{result['probe']}:{result['reason']}")
    manifest = dict(version=VERSION, release=dict(name=name, content_hash=content_hash),
                    handlers=sorted(set(digests)), verifier=verifier,
                    capture=dict(size_bytes=capture_size, cap_bytes=capture_cap),
                    min_authority=min_authority, built_from=sorted(set(built_from) | {commit[:12]}))
    return dict(id=unit_id(manifest), manifest=manifest)


# ---- worker report and roster comparison ----------------------------------------------------------

def report_part(*, release_hash=None, handler_digests=None, verifier_digest=None):
    """What a worker puts under report['unit']: closed keys, digests only."""
    return dict(release=release_hash, handlers=sorted(set(handler_digests or ())), verifier=verifier_digest)


def validate_report(value):
    """Authority-side validation of report['unit'] (bounded; raises WorkloadError)."""
    if (not isinstance(value, dict) or set(value) != {'release', 'handlers', 'verifier'}):
        _fail('unit_report_invalid')
    for key in ('release', 'verifier'):
        if value[key] is not None and (not isinstance(value[key], str) or not _HEX64.fullmatch(value[key])):
            _fail('unit_report_invalid')
    handlers = value['handlers']
    if (not isinstance(handlers, list) or len(handlers) > 256 or
            any(not isinstance(h, str) or not _HEX64.fullmatch(h) for h in handlers)):
        _fail('unit_report_invalid')
    return dict(release=value['release'], handlers=sorted(set(handlers)), verifier=value['verifier'])


def declared_parts(manifest):
    """The parts of a manifest in the same shape a worker reports them."""
    return dict(release=manifest['release']['content_hash'], handlers=list(manifest['handlers']),
                verifier=manifest['verifier'])


def unit_state(reported, desired):
    """`current`, `behind`, `unit_mismatch:<part>[,<part>]`, `unknown`, or `undeclared`.

    unknown: the worker reports no parts (legacy; it still serves). undeclared: nothing is desired.
    Parts that differ from the desired unit: all of them is `behind` (a coherent older unit); only some
    is `unit_mismatch` (parts from different units, e.g. new bundle with the old verifier). A desired
    verifier of None means the unit has none and the worker's verifier is not compared. The worker's
    handler set must CONTAIN the desired bundle (extra installed releases are normal until pruned)."""
    if desired is None:
        return 'undeclared', []
    if not reported:
        return 'unknown', []
    differing = []
    if reported.get('release') != desired['release']:
        differing.append('release')
    if not set(desired['handlers']) <= set(reported.get('handlers') or ()):
        differing.append('handlers')
    if desired['verifier'] is not None and reported.get('verifier') != desired['verifier']:
        differing.append('verifier')
    compared = 2 + (desired['verifier'] is not None)
    if not differing:
        return 'current', []
    if len(differing) == compared:
        return 'behind', differing
    return 'unit_mismatch:' + ','.join(differing), differing


_verifier_cache = {}


def worker_unit_report(verifier_dir, inventory, *, ttl_s=600, clock=None):
    """The `unit` report key for this worker, or None when it cannot be read (a worker that cannot say
    what it runs reports nothing, and the roster shows `unknown`; it never invents a digest).

    release: content_hash of the RELEASE.json beside the loaded livestack_node package (None if the
    worker runs from a source tree). handlers: digests of installed handler releases. verifier: tree
    digest of `verifier_dir`, cached `ttl_s` because it walks a directory."""
    import time
    now = (clock or time.monotonic)()
    try:
        meta = json.loads((Path(__file__).resolve().parents[2] / 'RELEASE.json').read_text())
        release = meta['content_hash'] if _HEX64.fullmatch(meta.get('content_hash', '')) else None
    except (OSError, ValueError, AttributeError):
        release = None
    verifier = None
    if verifier_dir:
        cached = _verifier_cache.get(verifier_dir)
        if cached is None or now - cached[0] > ttl_s:
            try:
                from .smoke import tree_digest
                cached = (now, tree_digest(verifier_dir))
            except OSError:
                cached = (now, None)
            _verifier_cache[verifier_dir] = cached
        verifier = cached[1]
    handlers = [r['release_digest'] for r in (inventory or {}).get('releases', [])]
    try:
        return validate_report(report_part(release_hash=release, handler_digests=handlers,
                                           verifier_digest=verifier))
    except WorkloadError:
        return None
