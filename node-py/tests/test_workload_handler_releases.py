"""Cross-repository v1 fixtures pin the handler package wire identity."""
import hashlib
import json
from pathlib import Path

import pytest

from livestack_node.workloads.handler_release import (
    MAX_PACKAGE_BYTES,
    validate_execution_envelope,
    validate_manifest,
    validate_result_identity,
    validate_worker_inventory,
)
from livestack_node.workloads.model import WorkloadError


FIXTURE = Path(__file__).parent / 'fixtures' / 'harmony-handler-release-v1'
RELEASE_DIGEST = '0c86a76fb84316c50bf284c4e7b53b3ff9767febc631c777d2c6799e1658f93b'
INPUT_DIGEST = '0' * 64


def manifest():
    return json.loads((FIXTURE / 'manifest.json').read_text())


def test_shared_v1_fixture_has_canonical_release_identity():
    value = manifest()
    result = validate_manifest(value, RELEASE_DIGEST)
    entry = value['files'][0]
    payload = (FIXTURE / entry['path']).read_bytes()
    assert len(payload) == entry['size']
    assert hashlib.sha256(payload).hexdigest() == entry['sha256']
    assert result['canonical_bytes'] == json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('mutate,reason', [
    (lambda value: value.update(format='harmony-handler-package.v99'), 'handler_package_format_unsupported'),
    (lambda value: value['files'][0].update(path='../handler.mjs'), 'handler_manifest_invalid_file_path'),
    (lambda value: value['files'][0].update(size=MAX_PACKAGE_BYTES + 1), 'handler_manifest_invalid_file_size'),
    (lambda value: value['files'][0].update(sha256='f' * 64), 'handler_release_digest_mismatch'),
    (lambda value: value['files'][0].update(mode=420.0), 'handler_manifest_invalid_file_mode'),
])
def test_unknown_or_malformed_package_is_refused(mutate, reason):
    value = manifest()
    mutate(value)
    expected = RELEASE_DIGEST if reason == 'handler_release_digest_mismatch' else None
    with pytest.raises(WorkloadError, match=reason):
        validate_manifest(value, expected)


def test_worker_report_assignment_and_result_share_exact_identity():
    release = validate_manifest(manifest())['release_digest']
    identity = dict(handler_id='benchday.fixture.e2e', release_digest=release,
                    execution_contract=1, payload_schema='benchday.test-train.request.v1',
                    result_schema='benchday.test-train.result.v1')
    inventory = validate_worker_inventory({'generation': 7, 'defaults': {'benchday.fixture.e2e': release},
                                            'releases': [identity]})
    assert inventory['defaults'] == {'benchday.fixture.e2e': release}
    assert inventory['releases'] == [identity]
    envelope = dict(job_id='job-1', attempt_id='attempt-1', fence=1, worker='worker-1',
                    boot='boot-1', handler_release=identity)
    assert validate_execution_envelope(envelope) == envelope
    result = {**envelope, 'input_digest': INPUT_DIGEST}
    assert validate_result_identity(result) == result
