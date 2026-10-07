"""labels.describe: a configurable per-handler requirement (default off), named error when missing."""
import hashlib

import pytest

from livestack_node.workloads.model import Limits, WorkloadError, submission

DIGEST = hashlib.sha256(b'x').hexdigest()


def _job(**extra):
    return dict(version=1, key='k1', handler='b.e2e.v1', input_digest=DIGEST, need={'cpu': 1}, **extra)


def test_default_is_off_so_existing_submitters_are_unaffected():
    assert submission(_job(), {'b.e2e.v1'}, Limits())['handler'] == 'b.e2e.v1'


def test_required_handler_without_a_description_is_refused_by_name():
    limits = Limits(describe_required_handlers=['b.e2e.v1'])
    for labels in ({}, {'describe': ''}, {'describe': '   '}):
        with pytest.raises(WorkloadError, match='job_description_required'):
            submission(_job(labels=labels), {'b.e2e.v1'}, limits)


def test_required_handler_with_a_description_and_other_handlers_pass():
    limits = Limits(describe_required_handlers=['b.e2e.v1'])
    spec = submission(_job(labels={'describe': 'admission 846f4ddaf: herdr-backend.x (+1)', 'origin': 'agent:claude@xc-tower-ubuntu:p3Q'}), {'b.e2e.v1'}, limits)
    assert spec['labels']['describe'].startswith('admission')
    assert submission(dict(_job(), handler='other.v1'), {'other.v1'}, limits)['handler'] == 'other.v1'


@pytest.mark.parametrize('bad', ['b.e2e.v1', [''], [1], 'x'])
def test_the_setting_must_be_a_list_of_handler_ids(bad):
    with pytest.raises(ValueError, match='describe_required_handlers'):
        Limits(describe_required_handlers=bad)


def test_origin_is_required_with_the_same_switch_and_shape_checked():
    limits = Limits(describe_required_handlers=['b.e2e.v1'])
    for origin in (None, '', 'someone', 'robot:x', 'agent:', 'agent:' + 'x' * 101):
        labels = {'describe': 'd'} if origin is None else {'describe': 'd', 'origin': origin}
        with pytest.raises(WorkloadError, match='job_origin_required'):
            submission(_job(labels=labels), {'b.e2e.v1'}, limits)
    for origin in ('human:acct_c082baa1', 'system:test-train-scheduler', 'agent:codex@h:w28:p3Q'):
        assert submission(_job(labels={'describe': 'd', 'origin': origin}), {'b.e2e.v1'}, limits)
