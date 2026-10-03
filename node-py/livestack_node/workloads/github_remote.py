"""Harmony-owned GitHub Actions dispatch and short-lived remote worker grants."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import stat
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .github_identity import API_ROOT, GitHubIdentityVerifier, MAX_API_BYTES
from .model import WorkloadError, encode, labels, name, resources


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def _read_private(path, *, maximum=16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum or
                info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077):
            raise WorkloadError('github_provider_credential_untrusted', 403)
        value = os.read(fd, maximum+1)
    finally:
        os.close(fd)
    if len(value) > maximum:
        raise WorkloadError('github_provider_credential_oversized', 403)
    return value


class GitHubApp:
    """Narrow GitHub App client: Actions read/write for one installation repo."""

    def __init__(self, config, *, opener=urlopen, clock=time.time):
        if (not isinstance(config, dict) or set(config) != {'app_id','installation_id','private_key_file'} or
                type(config['app_id']) is not int or config['app_id'] <= 0 or
                type(config['installation_id']) is not int or config['installation_id'] <= 0 or
                not isinstance(config['private_key_file'], str)):
            raise ValueError('invalid GitHub App configuration')
        self.config, self.opener, self.clock = config, opener, clock
        self._installation_token = None
        self._token_expiry = 0

    def _signing_key(self):
        try:
            key = serialization.load_pem_private_key(_read_private(self.config['private_key_file']), password=None)
            if not hasattr(key, 'sign'):
                raise ValueError('key')
            return key
        except WorkloadError:
            raise
        except Exception as error:
            raise WorkloadError('github_provider_credential_invalid', 403) from error

    def _jwt(self):
        now = int(self.clock())
        header = _b64(encode({'alg':'RS256','typ':'JWT'}).encode())
        claims = _b64(encode({'iss':str(self.config['app_id']), 'iat':now-30, 'exp':now+540}).encode())
        signing = (header+'.'+claims).encode()
        try:
            signature = self._signing_key().sign(signing, padding.PKCS1v15(), hashes.SHA256())
        except WorkloadError:
            raise
        except Exception as error:
            raise WorkloadError('github_provider_signing_failed', 503) from error
        return header+'.'+claims+'.'+_b64(signature)

    def _request(self, method, url, *, token, body=None, accepted=(200,)):
        headers = {'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',
                   'Authorization':'Bearer '+token}
        data = None if body is None else encode(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener(Request(url, data=data, headers=headers, method=method), timeout=10) as response:
                if response.status not in accepted:
                    raise WorkloadError('github_provider_api_refused', 503)
                raw = response.read(MAX_API_BYTES+1)
            if len(raw) > MAX_API_BYTES:
                raise WorkloadError('github_provider_api_oversized', 503)
            return (response.status, json.loads(raw) if raw else {})
        except WorkloadError:
            raise
        except HTTPError as error:
            # Status is named, response body and credentials are deliberately
            # excluded from logs and public errors.
            raise WorkloadError('github_provider_api_http_'+str(error.code), 503) from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise WorkloadError('github_provider_api_unavailable', 503) from error

    def token(self):
        if self._installation_token and self.clock() < self._token_expiry-120:
            return self._installation_token
        _, value = self._request('POST', f"{API_ROOT}/app/installations/{self.config['installation_id']}/access_tokens",
                                 token=self._jwt(), body={'permissions': {
                                     'actions':'write','contents':'read','metadata':'read'}},
                                 accepted=(201,))
        token = value.get('token')
        if not isinstance(token, str) or not token or not isinstance(value.get('expires_at'), str):
            raise WorkloadError('github_installation_token_invalid', 503)
        # GitHub installation tokens live for one hour. Cache for at most 50m;
        # parse failures stop dispatch instead of guessing a lifetime.
        import datetime
        try:
            expiry = datetime.datetime.fromisoformat(value['expires_at'].replace('Z','+00:00')).timestamp()
        except ValueError as error:
            raise WorkloadError('github_installation_token_expiry_invalid', 503) from error
        self._installation_token, self._token_expiry = token, min(expiry, self.clock()+3000)
        return token


class GitHubActionsProvider:
    def __init__(self, provider_id, config, token_key, *, opener=urlopen, clock=time.time):
        required = {'handlers','host','resources','labels','slots','workflow_path','workflow_ref','workflow_id',
                    'identity','app','max_seconds','compilation_classes'}
        if (not isinstance(config, dict) or set(config) != required or not isinstance(config['handlers'], list) or
                not config['handlers'] or len(set(config['handlers'])) != len(config['handlers']) or
                not isinstance(config['host'], str) or type(config['slots']) is not int or config['slots'] != 1 or
                not isinstance(config['workflow_path'], str) or not config['workflow_path'].startswith('.github/workflows/') or
                not isinstance(config['workflow_ref'], str) or not isinstance(config['identity'], dict) or
                type(config['workflow_id']) is not int or config['workflow_id'] <= 0 or
                type(config['max_seconds']) is not int or not 60 <= config['max_seconds'] <= 21600):
            raise ValueError('invalid GitHub Actions provider configuration')
        classes = config['compilation_classes']
        if (not isinstance(classes, list) or not classes or len(classes) != len(set(classes)) or
                any(not isinstance(value, str) or value not in
                    ('rust','flutter','image','node','native','apple','windows') for value in classes)):
            raise ValueError('invalid GitHub remote compilation classes')
        self.id = name(provider_id, 'GitHub provider')
        self.config = dict(config)
        self.config['resources'] = resources(config['resources'])
        self.config['labels'] = labels(config['labels'])
        if 'harmony.execution.provider' in self.config['labels']:
            raise ValueError('GitHub provider labels cannot override the provider identity')
        self.token_key = token_key
        if len(token_key) < 32:
            raise ValueError('remote worker token key must contain at least 32 bytes')
        self.identity = GitHubIdentityVerifier(config['identity'], clock=clock, opener=opener)
        workflow_parts = self.identity.config['workflow_ref'].rsplit('@', 1)
        workflow_ref = workflow_parts[1] if len(workflow_parts) == 2 else ''
        if (len(workflow_parts) != 2 or workflow_parts[0] !=
                self.identity.config['repository']+'/'+config['workflow_path'] or
                not workflow_ref.startswith('refs/tags/') or
                not workflow_ref.split('/',2)[2] or len(workflow_ref) > 200):
            raise ValueError('GitHub OIDC workflow must use an immutable tag ref and match the provider workflow path')
        self.dispatch_ref = workflow_ref.split('/',2)[2]
        self.app = GitHubApp(config['app'], opener=opener, clock=clock)
        self.opener, self.clock = opener, clock

    @property
    def handlers(self):
        return tuple(self.config['handlers'])

    def _api(self, method, path, *, body=None, accepted=(200,)):
        return self.app._request(method, API_ROOT+path, token=self.app.token(), body=body, accepted=accepted)

    def dispatch(self, item):
        spec = item['spec']
        # GitHub's dispatch API accepts a tag name, not a commit SHA. Refuse to
        # dispatch if the configured immutable tag has moved since operator
        # review, even when OIDC would reject the resulting run later.
        repo = self.identity.config['repository']
        _, ref = self._api('GET', f"/repos/{repo}/git/ref/tags/{quote(self.dispatch_ref, safe='/')}")
        target = ref.get('object') if isinstance(ref, dict) else None
        allowed_shas = self.identity.config['workflow_sha']
        if (not isinstance(target, dict) or target.get('type') != 'commit' or
                target.get('sha') not in allowed_shas):
            raise WorkloadError('github_workflow_revision_drift', 409)
        inputs = {'harmony_job_id':item['job_id'], 'harmony_correlation':item['correlation'],
                  'harmony_input_digest':spec['input_digest'], 'harmony_release_key':spec['key']}
        path = f"/repos/{repo}/actions/workflows/{self.config['workflow_id']}/dispatches"
        self._api('POST', path, body={'ref':self.dispatch_ref, 'inputs':inputs}, accepted=(204,))

    def find_run(self, correlation):
        repo = self.identity.config['repository']
        path = (f"/repos/{repo}/actions/workflows/{self.config['workflow_id']}/runs"
                "?event=workflow_dispatch&per_page=100")
        _, value = self._api('GET', path)
        runs = value.get('workflow_runs')
        if not isinstance(runs, list) or len(runs) > 100:
            raise WorkloadError('github_workflow_run_list_invalid', 503)
        title = self.identity.config['correlation_prefix']+correlation
        matches = [run for run in runs if isinstance(run, dict) and run.get('display_title') == title and
                   run.get('event') == self.identity.config['event_name'] and
                   run.get('workflow_id') == self.config['workflow_id']]
        if len(matches) > 1:
            raise WorkloadError('github_workflow_correlation_ambiguous', 409)
        if not matches:
            return None
        run = matches[0]
        if not isinstance(run.get('id'), int) or run.get('run_attempt') != 1:
            raise WorkloadError('github_workflow_run_identity_invalid', 403)
        return dict(run_id=str(run['id']), run_attempt=run['run_attempt'], status=run.get('status'),
                    conclusion=run.get('conclusion'))

    def run_status(self, run_id):
        repo = self.identity.config['repository']
        _, value = self._api('GET', f'/repos/{repo}/actions/runs/{run_id}')
        return dict(status=value.get('status'), conclusion=value.get('conclusion'))

    def cancel(self, run_id):
        repo = self.identity.config['repository']
        self._api('POST', f'/repos/{repo}/actions/runs/{run_id}/cancel', accepted=(202, 409))

    def issue_worker_token(self, context, identity):
        now = int(self.clock())
        claims = {'provider':self.id, 'job_id':context['job_id'], 'correlation':context['correlation'],
                  'run_id':identity['run_id'], 'run_attempt':identity['run_attempt'],
                  'worker':'gha-'+self.id, 'host':self.config['host'],
                  'boot':uuid.uuid4().hex,
                  'exp':min(now+self.config['max_seconds']+900, int(context['spec'].get('deadline') or now+86400))}
        raw = _b64(encode(claims).encode())
        mac = _b64(hmac.new(self.token_key, raw.encode(), hashlib.sha256).digest())
        return raw+'.'+mac, claims

    def principal(self, token):
        try:
            raw, supplied = token.split('.')
            expected = _b64(hmac.new(self.token_key, raw.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                return None
            claims = json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
            if (not isinstance(claims, dict) or claims.get('provider') != self.id or
                    type(claims.get('exp')) is not int or claims['exp'] <= self.clock()):
                return None
            from .http import Principal
            return Principal(id='gha-'+self.id+'-'+claims['run_id'], token=token, role='worker',
                             worker=claims['worker'], host=claims['host'], handlers=self.handlers,
                             remote_job=claims['job_id'], remote_provider=self.id,
                             remote_run_id=claims['run_id'], remote_boot=claims['boot'])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def bootstrap(self, store, body):
        allowed = {'job_id','correlation','input_digest','release_key','oidc_token','github_token'}
        if not isinstance(body, dict) or set(body) != allowed:
            raise WorkloadError('invalid GitHub worker bootstrap request')
        job_id, correlation = body['job_id'], body['correlation']
        if not isinstance(job_id, str) or len(job_id) != 32 or not isinstance(correlation, str) or len(correlation) != 32:
            raise WorkloadError('invalid GitHub worker correlation')
        if (not isinstance(body['input_digest'], str) or len(body['input_digest']) != 64 or
                any(char not in '0123456789abcdef' for char in body['input_digest']) or
                not isinstance(body['release_key'], str) or not 1 <= len(body['release_key']) <= 160):
            raise WorkloadError('invalid GitHub release identity')
        identity = self.identity.verify(body['oidc_token'], body['github_token'], correlation=correlation)
        store.remote_bind_run(self.id, job_id, correlation, identity['run_id'], identity['run_attempt'],
                              input_digest=body['input_digest'], release_key=body['release_key'])
        context = store.remote_job_context(self.id, job_id, correlation)
        if context['spec'].get('execution_provider') != self.id or context['spec']['handler'] not in self.handlers:
            raise WorkloadError('github_remote_handler_mismatch', 403)
        token, claims = self.issue_worker_token(context, identity)
        logging.info('github_remote_identity_verified: provider=%s repository=%s workflow=%s run_id=%s actor_id=%s job=%s',
                     self.id, identity['repository'], identity['workflow_ref'], identity['run_id'],
                     identity['actor_id'], job_id)
        return {'worker':claims['worker'], 'host':claims['host'], 'boot':claims['boot'], 'token':token,
                'job_id':job_id, 'run_id':identity['run_id'], 'run_attempt':identity['run_attempt'],
                'handler':context['spec']['handler'], 'input_digest':context['spec']['input_digest'],
                'release_key':context['spec']['key'], 'resources':self.config['resources'],
                'labels':self.config['labels'],
                'max_seconds':self.config['max_seconds']}

    def constrain_report(self, principal, report):
        if not isinstance(report, dict) or type(report.get('ready')) is not bool:
            raise WorkloadError('invalid remote worker report')
        observed = resources(report.get('available'))
        capacity = self.config['resources']
        return dict(capacity=capacity,
                    available={key:min(value, observed.get(key, 0)) for key,value in capacity.items()},
                    labels={**self.config['labels'], 'harmony.execution.provider':self.id},
                    handlers=list(self.handlers),
                    ready=bool(report.get('ready')))


class GitHubRemote:
    """Configuration and bounded dispatcher/reconciler for GitHub providers."""

    def __init__(self, config, *, opener=urlopen, clock=time.time, interval=5):
        if not isinstance(config, dict) or set(config) != {'token_key_file','providers','interval'}:
            raise ValueError('invalid github_remote configuration')
        key = _read_private(config['token_key_file'], maximum=4096)
        if len(key) < 32:
            raise ValueError('remote worker token key must contain at least 32 bytes')
        providers = config['providers']
        if not isinstance(providers, dict) or not 1 <= len(providers) <= 16:
            raise ValueError('configure 1..16 GitHub Actions providers')
        self.token_key = key
        self.providers = {name(k,'GitHub provider'):GitHubActionsProvider(k,v,key,opener=opener,clock=clock)
                          for k,v in providers.items()}
        self.handler_to_provider = {}
        for provider, instance in self.providers.items():
            for handler in instance.handlers:
                if handler in self.handler_to_provider:
                    raise ValueError('a workload handler may have only one GitHub provider')
                self.handler_to_provider[handler] = provider
        self.hosts = {key:value.config['host'] for key,value in self.providers.items()}
        self.interval = config['interval']
        if type(self.interval) not in (int,float) or not 1 <= self.interval <= 60:
            raise ValueError('GitHub provider interval must be in [1, 60]')
        self.store = None
        self.stop_event = threading.Event()
        self.thread = None

    def provider_for_handler(self, handler):
        return self.handler_to_provider.get(handler)

    def principal_for_token(self, token):
        for provider in self.providers.values():
            principal = provider.principal(token)
            if principal is not None:
                return principal
        return None

    def bootstrap(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('provider'), str):
            raise WorkloadError('GitHub provider identity required', 403)
        provider = self.providers.get(body['provider'])
        if provider is None:
            raise WorkloadError('GitHub provider is not configured', 403)
        return provider.bootstrap(self.store, {k:v for k,v in body.items() if k != 'provider'})

    def constrain_report(self, principal, report):
        provider = self.providers.get(principal.remote_provider)
        if provider is None:
            raise WorkloadError('GitHub provider is no longer configured', 403)
        return provider.constrain_report(principal, report)

    def start(self, store):
        if self.thread is not None:
            raise RuntimeError('GitHub dispatcher already started')
        self.store = store
        self.thread = threading.Thread(target=self._run, name='github-workload-provider', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as error:
                logging.exception('github_provider_reconcile_failed: %s', type(error).__name__)
            self.stop_event.wait(self.interval)

    def tick(self):
        for provider_id, provider in self.providers.items():
            item = self.store.reserve_remote_dispatch(provider_id, provider.config['slots'])
            if item is not None:
                try:
                    provider.dispatch(item)
                    self.store.remote_dispatch_unknown(item['job_id'], reason='dispatch accepted; run identity pending')
                    logging.info('github_remote_dispatch_accepted: provider=%s job=%s correlation=%s',
                                 provider_id, item['job_id'], item['correlation'])
                except Exception as error:
                    # Dispatch may have reached GitHub even when the response
                    # was lost. Keep the slot and reconcile by correlation.
                    self.store.remote_dispatch_unknown(item['job_id'], reason='dispatch outcome unknown: '+
                                                       type(error).__name__)
                    logging.warning('github_remote_dispatch_unknown: provider=%s job=%s cause=%s',
                                    provider_id, item['job_id'], type(error).__name__)
            rows = self.store.remote_dispatch_reconcile(provider_id)
            for row in rows:
                if row['run_id'] is None:
                    try:
                        found = provider.find_run(row['correlation'])
                        if found:
                            self.store.remote_bind_run(provider_id, row['job'], row['correlation'],
                                                       found['run_id'], found['run_attempt'])
                            row['run_id'], row['run_attempt'] = found['run_id'], found['run_attempt']
                            logging.info('github_remote_run_bound: provider=%s job=%s run_id=%s',
                                         provider_id, row['job'], found['run_id'])
                    except WorkloadError as error:
                        if error.status not in (409,):
                            logging.warning('github_remote_run_lookup_failed: provider=%s job=%s reason=%s',
                                            provider_id, row['job'], str(error))
                        continue
                if row['run_id'] is not None:
                    if row['state'] == 'cancel_requested' or row['job_state'] in ('cancelled','expired'):
                        try:
                            provider.cancel(row['run_id'])
                        except WorkloadError as error:
                            if error.status != 503:
                                logging.warning('github_remote_cancel_failed: provider=%s job=%s reason=%s',
                                                provider_id, row['job'], str(error))
                    try:
                        status = provider.run_status(row['run_id'])
                        self.store.remote_run_status(provider_id, row['job'], row['run_id'],
                                                     status['status'], status.get('conclusion'))
                    except WorkloadError as error:
                        logging.warning('github_remote_status_failed: provider=%s job=%s reason=%s',
                                        provider_id, row['job'], str(error))
            self.store.remote_finalize_cleanup()
