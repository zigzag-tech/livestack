"""Closed, bounded environment receipts attached to the producing attempt."""
from __future__ import annotations

import math
import re

from .model import WorkloadError, encode, name

MAX_REPLICA_BYTES = 32 * 1024**3
TIMING_PHASES = ('queue', 'transfer', 'source_materialization', 'dependencies', 'compile', 'test',
                 'execution', 'cleanup')
REUSE_OUTCOMES = ('created', 'reused', 'rebuilt', 'relocated')
CACHE_OUTCOMES = ('created', 'reused', 'rebuilt', 'relocated', 'invalidated')
REUSE_REASONS = ('created', 'compatible_environment_reused', 'source_updated_incrementally',
    'cache_inputs_changed', 'toolchain_changed', 'profile_changed', 'purpose_changed',
    'owner_scope_changed', 'local_state_untrusted', 'authority_replica_unconfirmed',
    'authority_replica_missing', 'authority_replica_host_mismatch',
    'authority_replica_profile_mismatch', 'authority_replica_compatibility_mismatch',
    'authority_replica_generation_mismatch', 'authority_replica_not_parked',
    'relocated_reconstructed', 'worker_environment_unavailable')


def validate(receipt, *, handle, generation, profile, source_digest):
    keys = {'version', 'handle', 'generation', 'profile', 'compatibility', 'source_digest',
            'reuse_outcome', 'reason_code', 'state', 'bytes_used', 'phase_timings', 'cache_components'}
    if not isinstance(receipt, dict) or set(receipt) != keys:
        raise WorkloadError('environment_receipt_invalid_fields', 400)
    if (type(receipt['version']) is not int or receipt['version'] != 1 or receipt['handle'] != handle or
            type(receipt['generation']) is not int or receipt['generation'] != generation or
            receipt['profile'] != profile or receipt['source_digest'] != source_digest):
        raise WorkloadError('environment_receipt_identity_mismatch', 409)
    compatibility = receipt['compatibility']
    if not isinstance(compatibility, str) or not re.fullmatch(r'[a-f0-9]{64}', compatibility):
        raise WorkloadError('environment_receipt_compatibility_invalid', 400)
    if (receipt['reuse_outcome'] not in REUSE_OUTCOMES or receipt['reason_code'] not in REUSE_REASONS or
            receipt['state'] not in ('parked', 'rebuild_required')):
        raise WorkloadError('environment_receipt_outcome_invalid', 400)
    used = receipt['bytes_used']
    if isinstance(used, bool) or not isinstance(used, int) or not 0 <= used <= MAX_REPLICA_BYTES:
        raise WorkloadError('environment_receipt_bytes_invalid', 400)
    timings = receipt['phase_timings']
    if not isinstance(timings, dict) or set(timings) != set(TIMING_PHASES):
        raise WorkloadError('environment_receipt_timing_phases_invalid', 400)
    for phase, value in timings.items():
        if not isinstance(value, dict) or set(value) not in ({'seconds'}, {'seconds', 'reason'}):
            raise WorkloadError('environment_receipt_timing_invalid', 400)
        seconds = value['seconds']
        reason = value.get('reason')
        if seconds is None:
            if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason):
                raise WorkloadError('environment_receipt_unknown_timing_needs_reason', 400)
        elif (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or
              not math.isfinite(seconds) or seconds < 0 or reason is not None):
            raise WorkloadError('environment_receipt_timing_invalid', 400)
    components = receipt['cache_components']
    if not isinstance(components, list) or len(components) > 16:
        raise WorkloadError('environment_receipt_cache_components_invalid', 400)
    seen = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != {'name', 'identity', 'outcome'}:
            raise WorkloadError('environment_receipt_cache_component_invalid', 400)
        component_name = name(component['name'], 'environment cache component')
        if (component_name in seen or not isinstance(component['identity'], str) or
                not re.fullmatch(r'[a-f0-9]{64}', component['identity']) or
                component['outcome'] not in CACHE_OUTCOMES):
            raise WorkloadError('environment_receipt_cache_component_invalid', 400)
        seen.add(component_name)
    encode(receipt, 16*1024)
    return receipt
