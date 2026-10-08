"""Filesystem-headroom admission, bounded GC and tiered retention
(openspec/changes/storage-headroom-admission). Every instrument has a control that moves the
measured quantity by a known amount: a fake filesystem whose free space is total minus the
bytes actually on disk, so a deletion really raises it."""
import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest

from livestack_node.workloads.blobs import BlobStore
from livestack_node.workloads.blob_references import BlobReferences
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.retention_tiers import RetentionTiers
from livestack_node.workloads.storage_bounds import HeadroomGuard, StorageBounds
from livestack_node.workloads.store import WorkloadStore

GIB = 1024**3


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeFs:
    """statvfs seam: free = total - bytes of regular files under root (+ `other`)."""
    def __init__(self, root, total, other=0):
        self.root, self.total, self.other, self.fail = root, total, other, False

    def __call__(self, path):
        if self.fail:
            raise OSError('boom')
        used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file()) + self.other
        return SimpleNamespace(f_blocks=self.total, f_frsize=1, f_bavail=self.total-used)


def make(tmp_path, *, total=1000, floor=100, other=0, clock=None, **blob):
    now = clock or [10_000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0])
    blobs = BlobStore(store, tmp_path/'objects', **blob)
    fs = FakeFs(tmp_path/'objects', total, other)
    mono = [0.0]
    bounds = StorageBounds.validate(dict(objects=dict(headroom_bytes=floor)))
    guard = HeadroomGuard(blobs.root, blobs.max_bytes, bounds, statvfs=fs, clock=lambda: mono[0])
    blobs.replace_policy(guard, None, 256)
    return store, blobs, fs, now, mono


def put(blobs, data, owner='alice'):
    blobs.put(owner, sha(data), len(data), BytesIO(data))
    return sha(data)


def test_floor_boundary_plus_minus_one_byte(tmp_path):
    # free = 1000 - 0 = 1000, floor 100: a 900 B object leaves exactly the floor -> admitted.
    store, blobs, fs, *_ = make(tmp_path)
    put(blobs, b'a'*900)
    # one byte more than the exact fit is refused, naming the figures, and writes nothing.
    store2, blobs2, *_ = make(tmp_path/'two')
    with pytest.raises(WorkloadError, match='storage_headroom: .*floor') as refused:
        put(blobs2, b'a'*901)
    assert refused.value.status == 507
    assert list((tmp_path/'two'/'objects').iterdir()) == []


def test_statvfs_failure_refuses_above_allowance_and_says_so(tmp_path):
    store, blobs, fs, *_ = make(tmp_path)
    fs.fail = True
    with pytest.raises(WorkloadError, match='storage_headroom_unknown') as refused:
        put(blobs, b'x'*(2*1024**2))
    assert refused.value.status == 507
    put(blobs, b'tiny')  # under the 1 MiB allowance
    assert blobs.guard.snapshot()['state'] == 'unknown'


def test_effective_cap_is_min_of_absolute_and_fraction(tmp_path):
    store, blobs, fs, *_ = make(tmp_path, max_bytes=800)
    bounds = StorageBounds.validate(dict(objects=dict(capacity_fraction=0.5, headroom_bytes=1)))
    blobs.replace_policy(HeadroomGuard(blobs.root, blobs.max_bytes, bounds, statvfs=fs), None, 256)
    assert blobs.guard.snapshot()['effective_max_bytes'] == 500
    with pytest.raises(WorkloadError, match='capacity'):
        put(blobs, b'a'*501)
    put(blobs, b'a'*500)


def test_missing_config_keeps_today_behaviour(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    blobs = BlobStore(store, tmp_path/'objects', max_bytes=10)
    assert blobs.effective_max_bytes == 10
    put(blobs, b'1234567890')


def test_state_low_then_refusing_logs_one_event_per_change(tmp_path):
    store, blobs, fs, *_ = make(tmp_path)  # floor 100, alert default 150
    assert blobs.guard.snapshot()['state'] == 'ok'
    fs.other = 860  # free 140
    assert blobs.guard.snapshot()['state'] == 'low'
    blobs.guard.snapshot()
    fs.other = 950  # free 50
    assert blobs.guard.snapshot()['state'] == 'refusing'
    assert [e['state'] for e in blobs.guard.events] == ['ok', 'low', 'refusing']


def test_gc_makes_room_once_per_window_and_never_touches_referenced(tmp_path):
    store, blobs, fs, now, mono = make(tmp_path)
    old = [put(blobs, bytes([i])*200) for i in range(3)]       # 600 B, unreferenced
    keep = put(blobs, b'k'*200)                                 # 200 B, referenced below
    BlobReferences(blobs).replace('alice', 'root', [keep], 0)
    now[0] += 7200
    # free = 1000-800 = 200; a 150 B put needs free-150 >= 100 -> deficit 50 -> one old object goes.
    put(blobs, b'n'*150)
    assert blobs.gc_runs == 1
    assert (blobs.root/keep).exists()
    assert sum(1 for d in old if (blobs.root/d).exists()) == 2  # oldest-first, stopped at the deficit
    # a second failing put inside the window must not collect again
    with pytest.raises(WorkloadError, match='storage_headroom'):
        put(blobs, b'm'*900)
    assert blobs.gc_runs == 1
    mono[0] += 100  # next window
    with pytest.raises(WorkloadError, match='storage_headroom'):
        put(blobs, b'q'*900)
    assert blobs.gc_runs == 2
    assert (blobs.root/keep).exists()


def test_only_referenced_bytes_remain_is_named_with_owners(tmp_path):
    store, blobs, fs, now, mono = make(tmp_path)
    digest = put(blobs, b'r'*900, owner='release')
    BlobReferences(blobs).replace('release', 'bd.release.1', [digest], 0)
    now[0] += 7200
    with pytest.raises(WorkloadError, match='all remaining bytes referenced.*release') as refused:
        put(blobs, b'z'*200, owner='bob')
    assert refused.value.status == 507 and (blobs.root/digest).exists()


def test_fresh_unreferenced_upload_is_not_collected(tmp_path):
    store, blobs, fs, now, mono = make(tmp_path)
    fresh = put(blobs, b'f'*900)           # just uploaded, job not submitted yet
    with pytest.raises(WorkloadError, match='storage_headroom'):
        put(blobs, b'g'*200)
    assert (blobs.root/fresh).exists()


# ---- tiers -----------------------------------------------------------------

def test_tier_validation_fails_closed_without_echoing_values():
    for bad in [dict(jobs=dict(failed_seconds=5)), dict(jobs=dict(nope=7200)), dict(extra=1),
                dict(references=[dict(owner='o', prefix='p', keep_newest=0, ttl_seconds=7200)]),
                dict(references=[dict(owner='o', prefix='p', keep_newest=1)]),
                dict(references=[dict(owner='o', prefix='p', keep_newest=1, ttl_seconds=None)])]:
        with pytest.raises(ValueError) as error:
            RetentionTiers.validate(bad)
        assert '7200' not in str(error.value) or 'Input' in str(error.value)
    RetentionTiers.validate(dict(references=[dict(owner='o', prefix='p', keep_newest=1, ttl_seconds=None,
                                                  keep_forever_acknowledged=True)]))
    RetentionTiers.validate(dict(references=[dict(owner='o', prefix='p', keep_newest=2, min_age_seconds=7200)]))


def job_rows(store, now, specs):
    with store.transaction() as db:
        for i, (state, age) in enumerate(specs):
            db.execute("INSERT INTO jobs(id,owner,request_key,request_hash,spec,state,created,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (f'j{i}', 'alice', f'k{i}', 'h', '{}', state, now-age, now-age))


def test_failed_jobs_outlive_succeeded_and_plan_deletes_nothing(tmp_path):
    now = [1_000_000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0],
                          limits=Limits(terminal_seconds=14*86400))
    store.retention_tiers = RetentionTiers.validate(dict(jobs=dict(succeeded_seconds=3*86400,
                                                                  failed_seconds=14*86400)))
    day = 86400
    job_rows(store, now[0], [('succeeded', 5*day), ('failed', 5*day), ('failed', 20*day), ('cancelled', 5*day)])
    plan = store.retention_plan()
    assert plan['jobs']['by_state'] == {'succeeded': 1, 'failed': 1}
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 4   # dry run deleted nothing
    store.sweep()
    with store.transaction() as db:
        left = {r[0] for r in db.execute('SELECT id FROM jobs')}
    assert left == {'j1', 'j3'}   # failed@5d and cancelled@5d (flat 14 d) survive


def test_release_references_keep_newest_and_ttl_and_unbounded_report(tmp_path):
    store, blobs, fs, now, mono = make(tmp_path, total=10**9)
    refs = BlobReferences(blobs)
    day = 86400
    for i in range(14):
        digest = put(blobs, f'rel{i}'.encode(), owner='release')
        refs.replace('release', f'bd.release.{i:02d}', [digest], 0)
        now[0] += day
    other = put(blobs, b'orphan-owner', owner='other')
    refs.replace('other', 'thing', [other], 0)
    blobs.replace_policy(blobs.guard, RetentionTiers.validate(dict(references=[
        dict(owner='release', prefix='bd.release.', keep_newest=10, ttl_seconds=5*day)])), 256)
    report = blobs.retention_plan()['references']
    # 14 refs, ages 14..1 d. Beyond the newest 10 are the 4 oldest (ages 14..11 d), all older than 5 d.
    assert report['would_expire']['count'] == 4
    assert report['unbounded_references']['owners'][0]['owner'] == 'other'
    assert report['unbounded_references']['count'] == 1
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM blob_references').fetchone()[0] == 15   # plan deleted nothing
    blobs.prune()
    with store.transaction() as db:
        names = sorted(r[0] for r in db.execute("SELECT name FROM blob_references WHERE owner='release'"))
    assert names == [f'bd.release.{i:02d}' for i in range(4, 14)]
    # the ttl guard: with a 30-day ttl nothing beyond the newest 10 is old enough
    blobs.replace_policy(blobs.guard, RetentionTiers.validate(dict(references=[
        dict(owner='release', prefix='bd.release.', keep_newest=2, ttl_seconds=30*day)])), 256)
    assert blobs.retention_plan()['references']['would_expire']['count'] == 0


def test_status_surfaces_bound_and_events(tmp_path):
    store, blobs, fs, *_ = make(tmp_path)
    status = blobs.status()
    assert status['bound']['floor_bytes'] == 100 and status['bound']['state'] == 'ok'
    assert status['unbounded_references']['count'] == 0
