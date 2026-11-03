"""Pure derivation over roster entries (no authority needed): the alert reads only the already
reported capacity/available pair, so a unit test over `_warnings` shows everything it can show;
the end-to-end roster shape is covered by test_storage_headroom_http.py."""
from livestack_node.workloads.roster import _warnings

GIB = 2**30


def entry(offered, capacity=120*GIB, **extra):
    base = dict(id='w', host='h', connected=True, state='idle', activation_failures=[],
                capacity=dict(disk_bytes=capacity), available=dict(disk_bytes=offered))
    return base | extra


def kinds(entries):
    return [w['kind'] for w in _warnings(entries, [])]


def test_warns_before_the_offer_reaches_zero():
    assert kinds([entry(50*GIB)]) == ['disk_offer_shrinking']
    assert 'offers 50.0GiB of 120.0GiB' in _warnings([entry(50*GIB)], [])[0]['detail']


def test_healthy_offer_and_exhausted_offer_are_not_this_warning():
    assert kinds([entry(100*GIB)]) == []
    # an offer of 0 is the existing reserve_exceeds_free warning, not a second one
    gone = dict(filesystem='/', free_bytes=96*GIB, reserve_bytes=100*GIB, capacity_bytes=120*GIB, reason='backing_reserve')
    assert kinds([entry(0, disk_unavailable=gone)]) == ['reserve_exceeds_free']


def test_disconnected_worker_is_silent():
    assert kinds([entry(10*GIB, connected=False)]) == []
