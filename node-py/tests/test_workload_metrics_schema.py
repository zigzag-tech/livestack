"""Metric definitions: undeclared and mis-scoped numbers are dropped and counted.

openspec/changes/measured-resource-declarations task 4.1 and design section 7 (metrics control).
"""
import pytest

from livestack_node.workloads import metrics_schema
from livestack_node.workloads.handler_release import validate_manifest
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore

H = 'compile.v1'
DEFINE = dict(name='firstMilestoneSeconds', unit='s', measures='seconds to the first run milestone',
              excludes='image build')


def test_unknown_names_nonfinite_and_negative_values_are_dropped_not_zeroed():
    accepted, undeclared, _ = metrics_schema.filter_metrics(
        {'attempt.execution_seconds': 3.0, 'postgresReady': 150, 'attempt.x': 1}, ())
    assert accepted == {'attempt.execution_seconds': 3.0} and undeclared == ['attempt.x', 'postgresReady']
    for bad in (float('nan'), float('inf'), -1, True, '3'):
        assert metrics_schema.filter_metrics({'attempt.execution_seconds': bad}, ())[0] == {}


def test_a_handler_declares_its_own_metric_in_the_manifest_definition():
    assert metrics_schema.filter_metrics({'firstMilestoneSeconds': 150}, [DEFINE])[0] == {'firstMilestoneSeconds': 150}
    assert metrics_schema.filter_metrics({'firstMilestoneSeconds': 150}, [])[0] == {}


def test_a_cache_only_metric_fed_a_whole_attempt_value_is_caught():
    # Positive control for the 2026-10-08 bug: docker_cache.seconds reported the whole attempt.
    ok, _, bad_ok = metrics_schema.filter_metrics({'docker_cache.seconds': 4.0, 'docker_cache.session_seconds': 300.0}, ())
    assert ok['docker_cache.seconds'] == 4.0 and bad_ok == []
    ok, _, mis = metrics_schema.filter_metrics({'docker_cache.seconds': 300.0, 'docker_cache.session_seconds': 120.0}, ())
    assert mis == ['docker_cache.seconds'] and 'docker_cache.seconds' not in ok


@pytest.mark.parametrize('metrics', ['x', [dict(DEFINE, extra=1)], [dict(DEFINE, unit='')], [DEFINE, DEFINE],
                                     [dict(DEFINE, name='attempt.execution_seconds')]])
def test_invalid_manifest_metric_definitions_are_refused(metrics):
    with pytest.raises(WorkloadError):
        metrics_schema.validate_manifest_metrics(metrics)


def test_store_counts_what_it_drops_and_shows_the_names(tmp_path):
    store = WorkloadStore(tmp_path/'a.db', handlers={H}, clock=lambda: 1.0)
    result = store._declared_metrics({'metrics': {'attempt.execution_seconds': 2, 'typo.seconds': 1}}, None)
    assert result['metrics'] == {'attempt.execution_seconds': 2}
    status = store.status()['metrics']
    assert status['undeclared_total'] == 1 and status['recent_dropped'] == ['typo.seconds']
    assert store._declared_metrics({'exit_code': 0}, None) == {'exit_code': 0}  # no metrics, untouched


def test_manifest_metrics_are_optional_validated_and_part_of_the_release_identity():
    import json
    from pathlib import Path
    base = json.loads((Path(__file__).parent/'fixtures'/'harmony-handler-release-v1'/'manifest.json').read_text())
    plain = validate_manifest(base)['release_digest']
    assert plain == '0c86a76fb84316c50bf284c4e7b53b3ff9767febc631c777d2c6799e1658f93b'  # old releases keep their digest
    declared = validate_manifest(dict(base, metrics=[DEFINE]))
    assert declared['release_digest'] != plain
    with pytest.raises(WorkloadError):
        validate_manifest(dict(base, metrics=[dict(DEFINE, unit=3)]))
