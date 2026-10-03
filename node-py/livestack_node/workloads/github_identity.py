"""Verify a GitHub Actions OIDC job against its live GitHub run.

The token is accepted only at the remote-worker bootstrap boundary. It is
never logged or persisted. Repository/workflow/run values are cross-checked
against operator configuration and the GitHub Actions API before a workload
credential can be issued.
"""
from __future__ import annotations

import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .model import WorkloadError


MAX_TOKEN_BYTES = 16 * 1024
MAX_API_BYTES = 1024 * 1024
JWKS_URL = 'https://token.actions.githubusercontent.com/.well-known/jwks'
API_ROOT = 'https://api.github.com'


def _b64url(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class GitHubIdentityVerifier:
    """Closed verifier for one configured repository/workflow identity."""

    def __init__(self, config, *, clock=time.time, opener=urlopen):
        required = {'repository', 'repository_id', 'workflow_ref', 'workflow_id',
                    'workflow_sha', 'event_name', 'job_name', 'audience',
                    'correlation_prefix', 'actor_ids'}
        if not isinstance(config, dict) or set(config) - required - {'actor_ids'} or not required <= set(config):
            raise ValueError('invalid GitHub remote identity configuration')
        if (not isinstance(config['repository'], str) or config['repository'].count('/') != 1 or
                not str(config['repository_id']).isdigit() or type(config['workflow_id']) is not int or
                config['workflow_id'] <= 0 or not isinstance(config['workflow_ref'], str) or
                not config['workflow_ref'].startswith(config['repository']+'/.github/workflows/') or
                not isinstance(config['workflow_sha'], list) or not config['workflow_sha'] or
                any(not isinstance(x, str) or len(x) != 40 for x in config['workflow_sha']) or
                not isinstance(config['event_name'], str) or not isinstance(config['job_name'], str) or
                not isinstance(config['audience'], str) or not config['audience'] or
                not isinstance(config['correlation_prefix'], str) or len(config['correlation_prefix']) > 64):
            raise ValueError('invalid GitHub remote identity configuration')
        actors = config['actor_ids']
        if not isinstance(actors, list) or not 1 <= len(actors) <= 32 or any(not str(x).isdigit() for x in actors):
            raise ValueError('invalid GitHub remote actor allowlist')
        self.config = dict(config, actor_ids=[str(x) for x in actors])
        self.clock = clock
        self.opener = opener
        self._keys = None
        self._keys_expiry = 0

    def _json_request(self, url, *, token=None):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with self.opener(Request(url, headers=headers), timeout=8) as response:
                if response.status != 200:
                    raise WorkloadError('github_identity_api_refused', 403)
                raw = response.read(MAX_API_BYTES + 1)
            if len(raw) > MAX_API_BYTES:
                raise WorkloadError('github_identity_api_oversized', 403)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise WorkloadError('github_identity_api_invalid', 403)
            return value
        except WorkloadError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise WorkloadError('github_identity_api_unavailable', 503) from error

    def _jwks(self):
        if self._keys is None or self.clock() >= self._keys_expiry:
            value = self._json_request(JWKS_URL)
            keys = value.get('keys')
            if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
                raise WorkloadError('github_oidc_signing_keys_invalid', 403)
            selected = {}
            for key in keys:
                if (not isinstance(key, dict) or key.get('kty') != 'RSA' or key.get('alg') != 'RS256' or
                        not isinstance(key.get('kid'), str) or not key['kid'] or
                        not isinstance(key.get('n'), str) or not isinstance(key.get('e'), str)):
                    continue
                modulus, exponent = int.from_bytes(_b64url(key['n']), 'big'), int.from_bytes(_b64url(key['e']), 'big')
                selected[key['kid']] = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            if not selected:
                raise WorkloadError('github_oidc_signing_keys_invalid', 403)
            self._keys, self._keys_expiry = selected, self.clock() + 3600
        return self._keys

    def decode(self, token):
        if not isinstance(token, str) or len(token.encode()) > MAX_TOKEN_BYTES:
            raise WorkloadError('github_oidc_token_invalid', 403)
        try:
            parts = token.split('.')
            if len(parts) != 3:
                raise ValueError('shape')
            header, claims = json.loads(_b64url(parts[0])), json.loads(_b64url(parts[1]))
            if not isinstance(header, dict) or not isinstance(claims, dict) or header.get('alg') != 'RS256':
                raise ValueError('header')
            key = self._jwks().get(header.get('kid'))
            if key is None:
                raise WorkloadError('github_oidc_key_unknown', 403)
            key.verify(_b64url(parts[2]), (parts[0]+'.'+parts[1]).encode(), padding.PKCS1v15(), hashes.SHA256())
        except WorkloadError:
            raise
        except Exception as error:
            raise WorkloadError('github_oidc_signature_invalid', 403) from error
        now = self.clock()
        if (claims.get('iss') != 'https://token.actions.githubusercontent.com' or
                claims.get('aud') != self.config['audience'] or
                any(type(claims.get(field)) not in (int, float) for field in ('iat', 'nbf', 'exp')) or
                claims['nbf'] > now + 30 or claims['iat'] > now + 30 or claims['exp'] <= now or
                claims['exp'] - claims['iat'] > 600):
            raise WorkloadError('github_oidc_claim_window_invalid', 403)
        return claims

    def verify(self, oidc_token, github_token, *, correlation):
        if not isinstance(github_token, str) or not 20 <= len(github_token) <= 4096:
            raise WorkloadError('github_api_token_missing', 403)
        claims = self.decode(oidc_token)
        required = {'repository', 'repository_id', 'workflow_ref', 'workflow_sha', 'event_name',
                    'run_id', 'run_attempt', 'actor_id', 'sha'}
        if (not required <= set(claims) or claims['repository'] != self.config['repository'] or
                str(claims['repository_id']) != str(self.config['repository_id']) or
                claims['workflow_ref'] != self.config['workflow_ref'] or
                claims.get('job_workflow_ref', claims['workflow_ref']) != self.config['workflow_ref'] or
                claims['workflow_sha'] not in self.config['workflow_sha'] or
                claims['event_name'] != self.config['event_name'] or
                str(claims['actor_id']) not in self.config['actor_ids'] or
                not str(claims['run_id']).isdigit() or type(claims['run_attempt']) is not int or
                claims['run_attempt'] < 1 or not isinstance(correlation, str) or len(correlation) != 32):
            raise WorkloadError('github_oidc_identity_mismatch', 403)

        run_id = str(claims['run_id'])
        repo = quote(self.config['repository'], safe='/')
        run = self._json_request(f'{API_ROOT}/repos/{repo}/actions/runs/{run_id}', token=github_token)
        if (str(run.get('id')) != run_id or run.get('workflow_id') != self.config['workflow_id'] or
                run.get('run_attempt') != claims['run_attempt'] or run.get('event') != self.config['event_name'] or
                run.get('status') != 'in_progress' or run.get('head_sha') != claims['workflow_sha'] or
                run.get('head_sha') != claims['sha'] or
                run.get('display_title') != self.config['correlation_prefix'] + correlation or
                str((run.get('actor') or {}).get('id')) != str(claims['actor_id'])):
            raise WorkloadError('github_actions_run_mismatch', 403)
        jobs = self._json_request(f'{API_ROOT}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100',
                                  token=github_token)
        records = jobs.get('jobs')
        if not isinstance(records, list) or len(records) > 100:
            raise WorkloadError('github_actions_jobs_invalid', 403)
        live = [item for item in records if isinstance(item, dict) and item.get('name') == self.config['job_name']
                and item.get('status') == 'in_progress' and item.get('conclusion') is None]
        if len(live) != 1:
            raise WorkloadError('github_actions_job_not_live', 403)
        return dict(repository=self.config['repository'], repository_id=str(self.config['repository_id']),
                    workflow_ref=self.config['workflow_ref'], workflow_sha=claims['workflow_sha'],
                    run_id=run_id, run_attempt=claims['run_attempt'], actor_id=str(claims['actor_id']),
                    workflow_job_id=str(live[0].get('id')))
