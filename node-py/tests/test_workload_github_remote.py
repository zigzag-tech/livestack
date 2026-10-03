import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.model import Limits
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.github_remote import GitHubActionsProvider, GitHubApp


def spec(key, *, handler='release.v1'):
    return dict(version=1, key=key, handler=handler, input_digest='a'*64,
                need={'cpu':4,'memory_bytes':8*1024**3,'disk_bytes':24*1024**3},
                estimate_seconds=1800, payload={'build':key})


def remote_store(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'authority.db', handlers={'release.v1','local.v1'},
                          clock=lambda:now[0], execution_providers={'release.v1':'github-actions'},
                          remote_hosts={'github-actions':'github-actions-macos'})
    return store, now


def report():
    capacity={'cpu':4,'memory_bytes':8*1024**3,'disk_bytes':24*1024**3}
    return dict(capacity=capacity, available=capacity,
                labels={'harmony.execution.provider':'github-actions'},
                handlers=['release.v1'], ready=True)


def test_operator_handler_mapping_routes_only_the_configured_handler(tmp_path):
    store, _ = remote_store(tmp_path)
    remote = store.submit('publisher', spec('ios-1'))
    local = store.submit('publisher', spec('local-1', handler='local.v1'))
    assert remote['spec']['execution_provider'] == 'github-actions'
    assert remote['spec']['selector']['harmony.execution.provider'] == 'github-actions'
    assert local['spec'].get('execution_provider') is None
    assert 'harmony.execution.provider' not in local['spec']['selector']
    assert remote['remote_execution']['state'] == 'queued'
    with pytest.raises(WorkloadError, match='unsupported workload schema or fields'):
        store.submit('publisher', dict(spec('caller-choice'), execution_provider='github-actions'))


def test_remote_dispatch_is_persisted_before_io_and_unknown_ack_holds_one_slot(tmp_path):
    store, now = remote_store(tmp_path)
    first = store.submit('publisher', spec('one'))
    now[0] += 1
    second = store.submit('publisher', spec('two'))
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    assert dispatch['job_id'] == first['id']
    assert len(dispatch['correlation']) == 32
    assert store.get('publisher', first['id'])['remote_execution']['state'] == 'dispatching'
    assert store.reserve_remote_dispatch('github-actions', 1) is None
    store.remote_dispatch_unknown(first['id'], reason='lost response')
    assert store.reserve_remote_dispatch('github-actions', 1) is None
    assert store.get('publisher', first['id'])['remote_execution']['correlation'] == dispatch['correlation']
    assert store.get('publisher', second['id'])['remote_execution']['state'] == 'queued'


def test_run_identity_binds_one_correlation_once_and_scopes_remote_claim(tmp_path):
    store, _ = remote_store(tmp_path)
    job = store.submit('publisher', spec('bound'))
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    store.remote_dispatch_unknown(job['id'])
    bound = store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '1234', 1)
    context = store.remote_job_context('github-actions', job['id'], dispatch['correlation'])
    assert context['spec']['input_digest'] == 'a'*64
    assert bound['remote_execution']['run_id'] == '1234'
    with pytest.raises(WorkloadError, match='already bound|replay'):
        store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '5678', 1)
    with pytest.raises(WorkloadError, match='github_run_identity_invalid'):
        store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '1234', 2)

    store.register('gha-1234','github-actions-macos','run-attempt-1',report())
    assert store.claim('gha-1234','run-attempt-1',job_id='0'*32) is None
    assignment = store.claim('gha-1234','run-attempt-1',job_id=job['id'])
    assert assignment['job_id'] == job['id'] and assignment['fence'] == 1


def test_cancel_holds_provider_capacity_through_run_and_worker_cleanup(tmp_path):
    store, _ = remote_store(tmp_path)
    job = store.submit('publisher', spec('cancel-me'))
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    store.remote_dispatch_unknown(job['id'])
    store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '3333', 1)
    store.register('gha-3333','github-actions-macos','run-attempt-1',report())
    attempt = store.claim('gha-3333','run-attempt-1',job_id=job['id'])
    store.cancel('publisher', job['id'])
    assert store.remote_cancel_requests('github-actions')[0]['run_id'] == '3333'
    store.remote_run_status('github-actions', job['id'], '3333', 'completed', 'cancelled')
    assert store.reserve_remote_dispatch('github-actions', 1) is None
    assert store.register('gha-3333','github-actions-macos','run-attempt-1',report(),
                          cleaned=[attempt['attempt_id']])['ready']
    store.remote_finalize_cleanup()
    assert store.get('publisher', job['id'])['remote_execution']['state'] == 'terminal'


def test_remote_run_without_harmony_completion_fails_closed(tmp_path):
    store, _ = remote_store(tmp_path)
    job = store.submit('publisher', spec('lost-worker'))
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    store.remote_dispatch_unknown(job['id'])
    store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '9999', 1)
    failed = store.remote_run_status('github-actions', job['id'], '9999', 'completed', 'success')
    assert failed['state'] == 'failed'
    assert 'without a Harmony completion' in failed['reason']
    assert failed['remote_execution']['state'] == 'terminal'


def test_deadline_cancellation_marks_the_remote_run_for_provider_cancel(tmp_path):
    store, now = remote_store(tmp_path)
    job = store.submit('publisher', {**spec('deadline'), 'deadline':now[0]+5, 'estimate_seconds':1})
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    store.remote_dispatch_unknown(job['id'])
    store.remote_bind_run('github-actions', job['id'], dispatch['correlation'], '12345', 1)
    now[0] += 6
    assert store.get('publisher', job['id'])['state'] == 'expired'
    assert store.remote_cancel_requests('github-actions')[0]['job'] == job['id']


def provider_config(tmp_path):
    return dict(handlers=['release.v1'], host='github-actions-macos',
        resources={'cpu':4,'memory_bytes':9*1024**3,'disk_bytes':12*1024**3}, slots=1,
        labels={'os':'macos','signing':'apple'},
        workflow_path='.github/workflows/release-ios-harmony.yml',
        workflow_ref='settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v1',
        workflow_id=77,
        identity={'repository':'settinghead/benchday','repository_id':'1234',
            'workflow_ref':'settinghead/benchday/.github/workflows/release-ios-harmony.yml@refs/tags/benchday-ios-remote-v1',
            'workflow_id':77,'workflow_sha':['a'*40],'event_name':'workflow_dispatch',
            'job_name':'Build and upload iOS','audience':'harmony',
            'correlation_prefix':'Harmony iOS release ','actor_ids':[42]},
        app={'app_id':12,'installation_id':34,'private_key_file':str(tmp_path/'unused.pem')},
        max_seconds=21600,compilation_classes=['apple','flutter','rust','native'])


def test_bootstrap_binds_the_dispatch_inputs_and_issues_only_a_job_scoped_grant(tmp_path):
    store, _ = remote_store(tmp_path)
    job = store.submit('publisher', spec('ios-42'))
    dispatch = store.reserve_remote_dispatch('github-actions', 1)
    store.remote_dispatch_unknown(job['id'])
    provider = GitHubActionsProvider('github-actions',provider_config(tmp_path),b'k'*32)
    provider.identity.verify = lambda _oidc, _github, *, correlation: {
        'repository':'settinghead/benchday','repository_id':'1234','workflow_ref':provider.identity.config['workflow_ref'],
        'workflow_sha':'a'*40,'run_id':'9876','run_attempt':1,'actor_id':'42','workflow_job_id':'54321'}
    body = {'job_id':job['id'],'correlation':dispatch['correlation'],'input_digest':'a'*64,
            'release_key':'ios-42','oidc_token':'oidc-token','github_token':'github-api-token'}
    with pytest.raises(WorkloadError, match='github_remote_release_identity_mismatch'):
        provider.bootstrap(store,{**body,'input_digest':'b'*64})
    assert store.get('publisher',job['id'])['remote_execution']['run_id'] is None

    grant = provider.bootstrap(store,body)
    assert grant['job_id'] == job['id'] and grant['run_id'] == '9876'
    assert grant['input_digest'] == 'a'*64 and grant['release_key'] == 'ios-42'
    assert 'github-api-token' not in grant['token'] and 'oidc-token' not in grant['token']
    principal = provider.principal(grant['token'])
    assert principal.remote_job == job['id'] and principal.remote_run_id == '9876'
    assert principal.remote_provider == 'github-actions' and principal.role == 'worker'
    assert provider.principal(grant['token']+'x') is None


def test_dispatch_checks_the_pinned_tag_sha_before_passing_authority_identity(tmp_path):
    provider = GitHubActionsProvider('github-actions',provider_config(tmp_path),b'k'*32)
    calls = []
    def api(method, path, *, body=None, accepted=(200,)):
        calls.append((method,path,body,accepted))
        return (200, {'object': {'type':'commit','sha':'a'*40}}) if method == 'GET' else (204, {})
    provider._api = api
    item = {'job_id':'1'*32,'correlation':'2'*32,
            'spec':{'input_digest':'a'*64,'key':'ios-42'}}
    provider.dispatch(item)
    method,path,body,accepted = calls[0]
    assert method == 'GET' and path.endswith('/git/ref/tags/benchday-ios-remote-v1')
    method,path,body,accepted = calls[1]
    assert method == 'POST' and accepted == (204,)
    assert path.endswith('/actions/workflows/77/dispatches')
    assert body == {'ref':'benchday-ios-remote-v1','inputs':{'harmony_job_id':'1'*32,'harmony_correlation':'2'*32,
        'harmony_input_digest':'a'*64,'harmony_release_key':'ios-42'}}


def test_dispatch_refuses_a_moved_or_annotated_workflow_tag(tmp_path):
    provider = GitHubActionsProvider('github-actions',provider_config(tmp_path),b'k'*32)
    calls = []
    provider._api = lambda method, path, *, body=None, accepted=(200,): (
        calls.append((method,path)) or (200, {'object': {'type':'commit','sha':'b'*40}}))
    with pytest.raises(WorkloadError, match='github_workflow_revision_drift'):
        provider.dispatch({'job_id':'1'*32,'correlation':'2'*32,
                           'spec':{'input_digest':'a'*64,'key':'ios-42'}})
    assert len(calls) == 1 and calls[0][0] == 'GET'


def test_remote_report_is_clamped_to_operator_capacity(tmp_path):
    provider = GitHubActionsProvider('github-actions',provider_config(tmp_path),b'k'*32)
    principal = type('Principal',(),{'remote_job':'a'*32})()
    constrained = provider.constrain_report(principal,dict(ready=True,
        available={'cpu':16,'memory_bytes':64*1024**3,'disk_bytes':1024**4}))
    assert constrained['capacity'] == provider.config['resources']
    assert constrained['available'] == provider.config['resources']
    assert constrained['labels'] == {'os':'macos','signing':'apple',
                                     'harmony.execution.provider':'github-actions'}
    assert constrained['handlers'] == ['release.v1']


def test_installation_token_requests_only_actions_and_contents_permissions(tmp_path):
    app = GitHubApp(dict(app_id=12,installation_id=34,private_key_file=str(tmp_path/'unused.pem')),
                    clock=lambda:1000)
    app._jwt = lambda:'signed-app-jwt'
    calls = []
    def request(method,url,*,token,body=None,accepted=(200,)):
        calls.append((method,url,token,body,accepted))
        return 201, {'token':'installation-token','expires_at':'2030-01-01T00:00:00Z'}
    app._request = request
    assert app.token() == 'installation-token'
    assert calls == [('POST','https://api.github.com/app/installations/34/access_tokens',
                      'signed-app-jwt',
                      {'permissions':{'actions':'write','contents':'read','metadata':'read'}},
                      (201,))]


def test_terminal_job_cannot_release_provider_slot_before_github_run_cleanup(tmp_path):
    store, now = remote_store(tmp_path)
    store.limits = Limits(active_jobs=10,terminal_jobs=1,terminal_seconds=1)
    job = store.submit('publisher',spec('retention'))
    dispatch = store.reserve_remote_dispatch('github-actions',1)
    store.remote_dispatch_unknown(job['id'])
    store.remote_bind_run('github-actions',job['id'],dispatch['correlation'],'5555',1)
    with store.transaction() as db:
        db.execute("UPDATE jobs SET state='succeeded',updated=? WHERE id=?",(now[0]-10,job['id']))
        store._prune(db,now[0])
    assert store.get('publisher',job['id'])['remote_execution']['state'] == 'running'
    assert store.reserve_remote_dispatch('github-actions',1) is None
    store.remote_run_status('github-actions',job['id'],'5555','completed','success')
    store.remote_finalize_cleanup()
    with store.transaction() as db:
        store._prune(db,now[0])
    assert store.reserve_remote_dispatch('github-actions',1) is None
    with pytest.raises(WorkloadError,match='job not found'):
        store.get('publisher',job['id'])
