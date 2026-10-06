"""Real HTTP + SQLite coverage for one-object upload grants."""
import hashlib
import http.client
import json
from threading import Thread

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads import upload_grants as ug
from livestack_node.workloads.store import WorkloadStore

DATA = b'source archive bytes'
DIGEST = hashlib.sha256(DATA).hexdigest()
OWNER, OTHER = 'a'*32, 'b'*32


def make(tmp_path, now=None):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=(lambda: now[0]) if now else None) \
        if now else WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('zzops', OWNER, 'caller', ('test.v1',), upload_grants=True),
        Principal('plain', OTHER, 'caller', ('test.v1',)),
    ])
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(server, method, path, token, body=None, data=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    h = {'Authorization': f'Bearer {token}', **(headers or {})}
    payload = data
    if body is not None:
        payload, h['Content-Type'] = json.dumps(body).encode(), 'application/json'
    conn.request(method, '/v1/workloads/'+path, body=payload, headers=h)
    r = conn.getresponse()
    out = json.loads(r.read() or b'{}')
    conn.close()
    return r.status, out


@pytest.fixture
def server(tmp_path):
    s = make(tmp_path)
    yield s
    s.shutdown(); s.server_close()


def mint(server, **kw):
    return call(server, 'POST', 'upload-grants', OWNER,
                dict(request_id='r1', digest=DIGEST, size=len(DATA), **kw))


def put(server, grant, data=DATA, token=None, digest=DIGEST):
    return call(server, 'PUT', f"upload-grants/{grant['grant_id']}/objects/{digest}",
                token or grant['capability'], data=data)


def test_grant_round_trip_stores_in_owner_namespace_and_cannot_do_more(server):
    status, grant = mint(server)
    assert status == 200 and grant['state'] == 'issued' and len(grant['capability']) >= 40
    assert grant['upload_url'].endswith(f"/v1/workloads/upload-grants/{grant['grant_id']}/objects/{DIGEST}")
    assert put(server, grant) == (200, {'grant_id': grant['grant_id'], 'digest': DIGEST, 'size': len(DATA)})
    with server.blobs.open('zzops', DIGEST) as (stream, size):
        assert stream.read() == DATA
    status, st = call(server, 'GET', 'upload-grants/r1', OWNER)
    assert status == 200 and st['state'] == 'uploaded' and 'capability' not in st
    # The capability reads nothing and reaches no other route.
    cap = grant['capability']
    assert call(server, 'GET', f'objects/{DIGEST}', cap)[0] == 401
    assert call(server, 'POST', 'jobs', cap, {})[0] == 401
    assert call(server, 'POST', 'upload-grants', cap, {})[0] == 401
    assert call(server, 'GET', f"upload-grants/{grant['grant_id']}/objects/{DIGEST}", cap)[0] == 405
    # Repeat of a completed request returns the receipt, not a capability.
    status, again = mint(server)
    assert status == 200 and again['state'] == 'uploaded' and 'capability' not in again


def test_issue_authorization_and_validation(server):
    assert call(server, 'POST', 'upload-grants', OTHER, dict(request_id='r', digest=DIGEST, size=1))[0] == 403
    assert call(server, 'POST', 'upload-grants', 'x'*32, {})[0] == 401
    assert call(server, 'POST', 'upload-grants', OWNER,
                dict(request_id='r', digest=DIGEST, size=1, owner='plain'))[0] == 403
    assert mint(server, expires_in_seconds=ug.MAX_EXPIRY_SECONDS+1)[0] == 400
    assert call(server, 'POST', 'upload-grants', OWNER, dict(request_id='a/b', digest=DIGEST, size=1))[0] == 400
    assert call(server, 'POST', 'upload-grants', OWNER, dict(request_id='r', digest='zz', size=1))[0] == 400
    assert mint(server)[0] == 200
    status, err = call(server, 'POST', 'upload-grants', OWNER, dict(request_id='r1', digest=DIGEST, size=1))
    assert status == 409 and 'binding' in err['error']
    assert call(server, 'GET', 'upload-grants/r1', OTHER)[0] == 403
    assert call(server, 'GET', 'upload-grants/missing', OWNER)[0] == 404


def test_capability_is_bound_to_digest_and_exact_size_and_failed_uploads_leave_nothing(server):
    _, grant = mint(server)
    other = hashlib.sha256(b'other').hexdigest()
    assert put(server, grant, digest=other)[0] == 403
    assert put(server, grant, data=DATA+b'x')[0] == 403
    assert put(server, grant, data=DATA[:-1])[0] == 403
    assert put(server, grant, token='nope')[0] == 401
    status, _ = put(server, grant, data=b'x'*len(DATA))  # right size, wrong bytes
    assert status == 409
    assert call(server, 'GET', 'upload-grants/r1', OWNER)[1]['state'] == 'issued'
    assert list(server.blobs.root.iterdir()) == []
    assert put(server, grant)[0] == 200  # the grant survives failed attempts


def test_lost_reply_reconciles_from_cas_without_second_transfer(tmp_path):
    s = make(tmp_path)
    try:
        _, grant = mint(s)
        s.blobs.put('zzops', DIGEST, len(DATA), __import__('io').BytesIO(DATA))  # CAS commit, no grant mark
        status, st = call(s, 'GET', 'upload-grants/r1', OWNER)
        assert st['state'] == 'uploaded' and st['grant_id'] == grant['grant_id']
    finally:
        s.shutdown(); s.server_close()


def test_expiry_rotation_revocation_and_durability(tmp_path):
    now = [1000.0]
    s = make(tmp_path, now)
    try:
        _, first = mint(s, expires_in_seconds=10)
        _, second = mint(s, expires_in_seconds=10)
        assert second['grant_id'] == first['grant_id'] and second['capability'] != first['capability']
        status, err = put(s, first)
        assert status == 403 and 'revoked' in err['error']
        now[0] += 11
        status, err = put(s, second)
        assert status == 410 and 'expired' in err['error']
        assert call(s, 'GET', 'upload-grants/r1', OWNER)[1]['state'] == 'expired'
        _, third = mint(s)
        assert put(s, third)[0] == 200
    finally:
        s.shutdown(); s.server_close()
    # Durable across restart; only verifiers and bounded metadata are stored.
    reopened = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    with reopened.transaction() as db:
        rows = db.execute('SELECT * FROM upload_grants').fetchall()
        dump = json.dumps([tuple(r) for r in db.execute('SELECT * FROM upload_grant_events')])
    assert [r['state'] for r in rows] == ['uploaded']
    assert third['capability'] not in json.dumps([tuple(r) for r in rows]) + dump
    outcomes = [e[0] for e in reopened.connect().execute('SELECT outcome FROM upload_grant_events ORDER BY id')]
    assert outcomes[:2] == ['issued', 'rotated'] and 'refused_revoked' in outcomes and outcomes[-1] == 'uploaded'


def test_capacity_bounds_and_event_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(ug, 'MAX_UNEXPIRED_PER_OWNER', 2)
    monkeypatch.setattr(ug, 'MAX_EVENTS', 5)
    s = make(tmp_path)
    try:
        for i in range(2):
            assert call(s, 'POST', 'upload-grants', OWNER, dict(request_id=f'q{i}', digest=DIGEST, size=1))[0] == 200
        status, err = call(s, 'POST', 'upload-grants', OWNER, dict(request_id='q9', digest=DIGEST, size=1))
        assert status == 429 and 'capacity' in err['error']
        assert call(s, 'GET', 'upload-grants/q0', OWNER)[0] == 200  # earlier grants preserved
        with s.store.transaction() as db:
            assert db.execute('SELECT count(*) FROM upload_grant_events').fetchone()[0] <= 6
    finally:
        s.shutdown(); s.server_close()


def test_concurrent_upload_is_refused_in_use(server):
    _, grant = mint(server)
    server.upload_grants._active.add(grant['grant_id'])
    assert put(server, grant)[0] == 409
    assert mint(server)[1]['error'] == 'grant_in_use'
    server.upload_grants._active.clear()
    assert put(server, grant)[0] == 200


def test_principal_flag_validation():
    with pytest.raises(ValueError):
        Principal('w', 'w'*32, 'worker', worker='w', host='h', upload_grants=True)
    with pytest.raises(ValueError):
        Principal('c', 'c'*32, 'caller', ('h',), upload_grants='yes')


def test_a_principal_upload_base_url_is_used_only_for_its_own_grants(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('benchday', OTHER, 'caller', ('test.v1',), upload_grants=True),
        Principal('collab', OWNER, 'caller', ('test.v1',), upload_grants=True, upload_base_url='https://relay.example/')])
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        _, mine = call(server, 'POST', 'upload-grants', OWNER, dict(request_id='r1', digest=DIGEST, size=len(DATA)))
        _, other = call(server, 'POST', 'upload-grants', OTHER, dict(request_id='r1', digest=DIGEST, size=len(DATA)))
        assert mine['upload_url'].startswith('https://relay.example/v1/workloads/upload-grants/')
        assert other['upload_url'].startswith('http://127.0.0.1:'), 'another principal keeps the authority\'s own address'
        assert call(server, 'PUT', f"upload-grants/{mine['grant_id']}/objects/{DIGEST}", mine['capability'], data=DATA)[0] == 200
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.parametrize('bad', ['relay.example', 'https://relay.example/path', 'https://u:p@relay.example', 'ftp://relay.example'])
def test_upload_base_url_must_be_an_origin_and_needs_upload_grants(bad):
    with pytest.raises(ValueError):
        Principal('x', OWNER, 'caller', ('test.v1',), upload_grants=True, upload_base_url=bad)
    with pytest.raises(ValueError):
        Principal('x', OWNER, 'caller', ('test.v1',), upload_base_url='https://relay.example')


def test_a_global_public_base_url_with_several_grant_principals_is_refused_by_name():
    from livestack_node.workloads.http import check_grant_origins
    one = [Principal('a', OWNER, 'caller', ('test.v1',), upload_grants=True)]
    two = one + [Principal('b', OTHER, 'caller', ('test.v1',), upload_grants=True)]
    check_grant_origins(one, 'https://relay.example')   # a single minting principal: unambiguous
    check_grant_origins(two, None)
    with pytest.raises(ValueError, match='applies to every grant-minting principal \\(a, b\\)'):
        check_grant_origins(two, 'https://relay.example')
