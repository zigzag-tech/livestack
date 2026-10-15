"""Explicit internal-testing acceptance; never a measured qualification result."""
import hashlib
import json

from .contract import ContractError


def validate_operator_waiver(raw, *, profile_id, engine):
    """Validate bounded, privately loaded operator receipt against exact identity."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractError('profile_unavailable', 'invalid operator waiver receipt', 503) from exc
    keys = {'version', 'decision', 'scope', 'operator', 'profile_id', 'unit',
            'model_revision', 'tokenizer_hash', 'reason'}
    valid = (isinstance(value, dict) and set(value) == keys
        and type(value['version']) is int and value['version'] == 1
        and value['decision'] == 'waive-qualification'
        and value['scope'] == 'internal-testing'
        and value['operator'] == 'settinghead'
        and value['profile_id'] == profile_id
        and value['unit'] == engine.unit
        and value['model_revision'] == engine.model_revision
        and value['tokenizer_hash'] == engine.tokenizer_hash
        and isinstance(value['reason'], str) and 1 <= len(value['reason']) <= 1000)
    marker = 'operator-waiver:sha256:' + hashlib.sha256(raw).hexdigest()
    if not valid or engine.qualification_hash != marker:
        raise ContractError('profile_unavailable', 'operator waiver does not match engine identity and receipt', 503)
    return marker
