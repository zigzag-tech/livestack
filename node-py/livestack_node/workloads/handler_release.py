"""Closed v1 contract for independently released Harmony command handlers."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .model import WorkloadError

FORMAT = 'harmony-handler-package.v1'
MAX_PACKAGE_BYTES = 2 * 1024**3
MAX_PACKAGE_FILES = 10_000
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_OUTPUTS = 128
MAX_WORKER_RELEASES = 256
_HANDLER = re.compile(r'^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$')
_IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9._-]{0,127}$')
_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_MANIFEST_KEYS = {
    'format', 'handler_id', 'release_version', 'execution_contract',
    'payload_schema', 'result_schema', 'platform', 'architecture', 'backend',
    'runtime_id', 'entrypoint', 'arguments', 'outputs', 'infrastructure_outputs',
    'infrastructure_exit_codes', 'files',
}


def _fail(reason):
    raise WorkloadError(reason, 400)


def _string(value, field, pattern=None, maximum=256):
    if (not isinstance(value, str) or not value or len(value) > maximum or
            unicodedata.normalize('NFC', value) != value or '\x00' in value):
        _fail(f'handler_manifest_invalid_{field}')
    if pattern is not None and not pattern.fullmatch(value):
        _fail(f'handler_manifest_invalid_{field}')
    return value


def _relative_path(value, field):
    value = _string(value, field, maximum=512)
    parts = value.split('/')
    if (value.startswith('/') or '\\' in value or ':' in value or
            any(part in ('', '.', '..') for part in parts) or
            any(ord(ch) < 32 for ch in value)):
        _fail(f'handler_manifest_invalid_{field}')
    return value


def _sorted_unique(values, field, *, maximum, validate):
    if not isinstance(values, list) or not values or len(values) > maximum:
        _fail(f'handler_manifest_invalid_{field}')
    parsed = [validate(item) for item in values]
    if len(set(parsed)) != len(parsed) or parsed != sorted(parsed, key=lambda item: item.encode('utf-8')):
        _fail(f'handler_manifest_noncanonical_{field}')
    return parsed


def validate_manifest(manifest, expected_digest=None):
    """Validate and return the canonical v1 manifest identity.

    The manifest deliberately contains no digest field: callers send the
    computed release digest beside the manifest, avoiding a self-hash.
    """
    if not isinstance(manifest, dict):
        _fail('handler_manifest_invalid_object')
    if manifest.get('format') != FORMAT:
        _fail('handler_package_format_unsupported')
    if set(manifest) != _MANIFEST_KEYS:
        _fail('handler_manifest_unknown_or_missing_fields')

    _string(manifest['format'], 'format')
    _string(manifest['handler_id'], 'handler_id', _HANDLER)
    _string(manifest['release_version'], 'release_version', _IDENTIFIER, 64)
    if type(manifest['execution_contract']) is not int or manifest['execution_contract'] != 1:
        _fail('handler_execution_contract_unsupported')
    _string(manifest['payload_schema'], 'payload_schema', _IDENTIFIER)
    _string(manifest['result_schema'], 'result_schema', _IDENTIFIER)
    if manifest['platform'] not in ('linux', 'darwin', 'windows'):
        _fail('handler_manifest_invalid_platform')
    if manifest['architecture'] not in ('x86_64', 'aarch64', 'arm64'):
        _fail('handler_manifest_invalid_architecture')
    if manifest['backend'] not in ('native', 'rootless-docker', 'rootless-docker-native'):
        _fail('handler_manifest_invalid_backend')
    _string(manifest['runtime_id'], 'runtime_id', _IDENTIFIER, 64)

    entrypoint = _relative_path(manifest['entrypoint'], 'entrypoint')
    arguments = manifest['arguments']
    if (not isinstance(arguments, list) or len(arguments) > 16 or
            any(not isinstance(argument, str) or len(argument) > 512 or '\x00' in argument or
                unicodedata.normalize('NFC', argument) != argument for argument in arguments)):
        _fail('handler_manifest_invalid_arguments')
    outputs_value = manifest['outputs']
    if not isinstance(outputs_value, list) or len(outputs_value) > MAX_OUTPUTS:
        _fail('handler_manifest_invalid_outputs')
    outputs = [_relative_path(value, 'output') for value in outputs_value]
    if len(set(outputs)) != len(outputs) or outputs != sorted(outputs, key=lambda item: item.encode('utf-8')):
        _fail('handler_manifest_noncanonical_outputs')
    infrastructure_value = manifest['infrastructure_outputs']
    if not isinstance(infrastructure_value, list) or len(infrastructure_value) > MAX_OUTPUTS:
        _fail('handler_manifest_invalid_infrastructure_outputs')
    infrastructure_outputs = [_relative_path(value, 'infrastructure_output') for value in infrastructure_value]
    if (len(set(infrastructure_outputs)) != len(infrastructure_outputs) or
            infrastructure_outputs != sorted(infrastructure_outputs, key=lambda item: item.encode('utf-8'))):
        _fail('handler_manifest_noncanonical_infrastructure_outputs')
    codes = manifest['infrastructure_exit_codes']
    if (not isinstance(codes, list) or len(codes) > 32 or
            any(type(code) is not int or not 1 <= code <= 255 for code in codes) or
            codes != sorted(set(codes))):
        _fail('handler_manifest_invalid_infrastructure_exit_codes')

    files = manifest['files']
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_PACKAGE_FILES:
        _fail('handler_manifest_invalid_file_inventory')
    paths = []
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'mode', 'size', 'sha256'}:
            _fail('handler_manifest_invalid_file_entry')
        path = _relative_path(item['path'], 'file_path')
        mode, size = item['mode'], item['size']
        if type(mode) is not int or mode not in (0o444, 0o555):
            _fail('handler_manifest_invalid_file_mode')
        if type(size) is not int or not 0 <= size <= MAX_PACKAGE_BYTES:
            _fail('handler_manifest_invalid_file_size')
        _string(item['sha256'], 'file_digest', _DIGEST, 64)
        paths.append(path)
        total += size
        if total > MAX_PACKAGE_BYTES:
            _fail('handler_package_bytes_exceeded')
    if len(set(paths)) != len(paths) or paths != sorted(paths, key=lambda item: item.encode('utf-8')):
        _fail('handler_manifest_noncanonical_file_inventory')
    if entrypoint not in set(paths):
        _fail('handler_manifest_entrypoint_missing')
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(canonical) > MAX_MANIFEST_BYTES:
        _fail('handler_manifest_bytes_exceeded')
    digest = hashlib.sha256(canonical).hexdigest()
    if expected_digest is not None and (not isinstance(expected_digest, str) or
                                        not _DIGEST.fullmatch(expected_digest) or
                                        digest != expected_digest):
        _fail('handler_release_digest_mismatch')
    return {'manifest': manifest, 'canonical_bytes': canonical, 'release_digest': digest}


def validate_release_identity(value):
    required = {'handler_id', 'release_digest', 'execution_contract', 'payload_schema', 'result_schema'}
    if not isinstance(value, dict) or set(value) != required:
        _fail('handler_release_identity_invalid')
    _string(value['handler_id'], 'handler_id', _HANDLER)
    _string(value['release_digest'], 'release_digest', _DIGEST, 64)
    if type(value['execution_contract']) is not int or value['execution_contract'] != 1:
        _fail('handler_execution_contract_unsupported')
    _string(value['payload_schema'], 'payload_schema', _IDENTIFIER)
    _string(value['result_schema'], 'result_schema', _IDENTIFIER)
    return dict(value)


def validate_worker_inventory(value):
    if not isinstance(value, dict) or set(value) != {'generation', 'defaults', 'releases'}:
        _fail('handler_worker_inventory_invalid')
    generation, defaults, releases = value['generation'], value['defaults'], value['releases']
    if (type(generation) is not int or generation < 0 or not isinstance(defaults, dict) or len(defaults) > 64 or
            not isinstance(releases, list) or len(releases) > MAX_WORKER_RELEASES):
        _fail('handler_worker_inventory_invalid')
    parsed = [validate_release_identity(item) for item in releases]
    keys = [(item['handler_id'], item['release_digest']) for item in parsed]
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        _fail('handler_worker_inventory_noncanonical')
    for handler, digest in defaults.items():
        _string(handler, 'handler_id', _HANDLER)
        _string(digest, 'release_digest', _DIGEST, 64)
        if (handler, digest) not in set(keys):
            _fail('handler_worker_inventory_default_missing')
    return {'generation': generation, 'defaults': dict(sorted(defaults.items())), 'releases': parsed}


def validate_execution_envelope(value):
    required = {'job_id', 'attempt_id', 'fence', 'worker', 'boot', 'handler_release'}
    if not isinstance(value, dict) or set(value) != required:
        _fail('handler_execution_envelope_invalid')
    for field in ('job_id', 'attempt_id', 'worker', 'boot'):
        _string(value[field], field, _IDENTIFIER, 128)
    if type(value['fence']) is not int or value['fence'] < 1:
        _fail('handler_execution_envelope_invalid_fence')
    identity = validate_release_identity(value['handler_release'])
    return {**value, 'handler_release': identity}


def validate_result_identity(value):
    required = {'job_id', 'attempt_id', 'fence', 'worker', 'boot', 'input_digest', 'handler_release'}
    if not isinstance(value, dict) or set(value) != required:
        _fail('handler_result_identity_invalid')
    envelope = validate_execution_envelope({key: value[key] for key in (
        'job_id', 'attempt_id', 'fence', 'worker', 'boot', 'handler_release')})
    _string(value['input_digest'], 'input_digest', _DIGEST, 64)
    return {**envelope, 'input_digest': value['input_digest']}
