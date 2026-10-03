import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from livestack_node.workloads.github_identity import GitHubIdentityVerifier, JWKS_URL
from livestack_node.workloads.model import WorkloadError


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def token(key, claims, *, kid='test-key'):
    header = b64(json.dumps({'alg': 'RS256', 'kid': kid}).encode())
    payload = b64(json.dumps(claims, separators=(',', ':')).encode())
    signing = (header+'.'+payload).encode()
    signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return header+'.'+payload+'.'+b64(signature)


@pytest.fixture
def verifier():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk = {'kty': 'RSA', 'alg': 'RS256', 'kid': 'test-key',
           'n': b64(numbers.n.to_bytes((numbers.n.bit_length()+7)//8, 'big')),
           'e': b64(numbers.e.to_bytes((numbers.e.bit_length()+7)//8, 'big'))}
    config = dict(repository='settinghead/benchday', repository_id='12345',
                  workflow_ref='settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/heads/main',
                  workflow_id=77, workflow_sha=['a'*40], event_name='workflow_dispatch',
                  job_name='Build and upload iOS', audience='harmony',
                  correlation_prefix='Harmony iOS release ', actor_ids=['42'])
    verifier = GitHubIdentityVerifier(config, clock=lambda: 1000)
    run_id = '999'
    verifier._json_request = lambda url, token=None: (
        {'keys': [jwk]} if url == JWKS_URL else
        {'id': 999, 'workflow_id': 77, 'run_attempt': 1, 'event': 'workflow_dispatch',
         'status': 'in_progress', 'head_sha': 'a'*40,
         'display_title': 'Harmony iOS release '+'c'*32, 'actor': {'id': 42}}
        if '/actions/runs/999' in url and '/jobs?' not in url else
        {'jobs': [{'id': 321, 'name': 'Build and upload iOS', 'status': 'in_progress', 'conclusion': None}]})
    claims = dict(iss='https://token.actions.githubusercontent.com', aud='harmony', iat=990, nbf=990, exp=1050,
                  repository='settinghead/benchday', repository_id=12345,
                  workflow_ref=config['workflow_ref'], workflow_sha='a'*40, event_name='workflow_dispatch',
                  run_id=run_id, run_attempt=1, actor_id=42, sha='a'*40)
    return verifier, private, claims


@pytest.mark.parametrize('attempt_claim',[1, '1'])
def test_valid_oidc_is_cross_checked_against_live_run_and_job(verifier, attempt_claim):
    service, private, claims = verifier
    claims['run_attempt'] = attempt_claim
    identity = service.verify(token(private, claims), 'g'*32, correlation='c'*32)
    assert identity == dict(repository='settinghead/benchday', repository_id='12345',
                            workflow_ref=service.config['workflow_ref'], workflow_sha='a'*40,
                            run_id='999', run_attempt=1, actor_id='42', workflow_job_id='321')


@pytest.mark.parametrize('field,value,reason', [
    ('repository', 'attacker/benchday', 'github_oidc_identity_mismatch'),
    ('event_name', 'pull_request', 'github_oidc_identity_mismatch'),
    ('workflow_ref', 'settinghead/benchday/.github/workflows/other.yml@refs/heads/main', 'github_oidc_identity_mismatch'),
    ('workflow_sha', 'b'*40, 'github_oidc_identity_mismatch'),
    ('actor_id', 43, 'github_oidc_identity_mismatch'),
    ('run_attempt', 2, 'github_actions_run_mismatch'),
    ('run_attempt', '01', 'github_oidc_identity_mismatch'),
    ('run_attempt', '1.0', 'github_oidc_identity_mismatch'),
])
def test_oidc_identity_mismatch_is_refused(verifier, field, value, reason):
    service, private, claims = verifier
    claims[field] = value
    with pytest.raises(WorkloadError, match=reason):
        service.verify(token(private, claims), 'g'*32, correlation='c'*32)


def test_bad_signature_is_refused(verifier):
    service, _, claims = verifier
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(WorkloadError, match='github_oidc_signature_invalid'):
        service.verify(token(other, claims), 'g'*32, correlation='c'*32)


def test_expired_or_overlong_oidc_window_is_refused(verifier):
    service, private, claims = verifier
    claims['exp'] = 999
    with pytest.raises(WorkloadError, match='github_oidc_claim_window_invalid'):
        service.decode(token(private, claims))
    claims['exp'], claims['iat'] = 1800, 900
    with pytest.raises(WorkloadError, match='github_oidc_claim_window_invalid'):
        service.decode(token(private, claims))


@pytest.mark.parametrize('change,reason', [
    ({'status': 'completed'}, 'github_actions_run_mismatch'),
    ({'display_title': 'manual run'}, 'github_actions_run_mismatch'),
    ({'jobs': []}, 'github_actions_job_not_live'),
])
def test_github_api_must_confirm_the_correlated_active_job(verifier, change, reason):
    service, private, claims = verifier
    original = service._json_request

    def api(url, token=None):
        value = original(url, token=token)
        if '/actions/runs/999' in url and '/jobs?' not in url:
            value.update({k: v for k, v in change.items() if k != 'jobs'})
        elif 'jobs?' in url and 'jobs' in change:
            value['jobs'] = change['jobs']
        return value

    service._json_request = api
    with pytest.raises(WorkloadError, match=reason):
        service.verify(token(private, claims), 'g'*32, correlation='c'*32)


def test_config_rejects_unbounded_workflow_identity():
    with pytest.raises(ValueError, match='invalid GitHub remote identity configuration'):
        GitHubIdentityVerifier({'repository': 'settinghead/benchday'})
