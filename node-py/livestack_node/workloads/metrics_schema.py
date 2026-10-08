"""Definitions for the numbers a result may report (openspec measured-resource-declarations).

A number with no definition is a number nobody can interpret: `docker_cache.seconds` once
meant the whole attempt, and `postgresReady` once meant the first run milestone. Every
metric names its unit, what it measures and what it EXCLUDES, and a sub-phase must not
exceed the whole it sits inside. Worker metrics are declared here; a handler declares its
own in its release manifest (`metrics: [{name, unit, measures, excludes}]`).
"""
import math
import re

from .model import WorkloadError

MAX_METRICS = 64
MAX_UNDECLARED_SHOWN = 8
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_.]{0,63}$')
_TEXT = 240
_TOLERANCE = 1.05  # rounding of the reporters (tenths of a second), not slack for mis-scoping

# name -> definition. `within`: the enclosing whole this value must not exceed.
BUILTIN = {
    'attempt.execution_seconds': dict(unit='s', measures='wall seconds from launching the attempt unit to its exit receipt',
                                      excludes='input transfer, source materialization, artifact upload, cleanup'),
    'docker_cache.session_seconds': dict(unit='s', measures='wall seconds of the whole attempt inside the Docker launcher',
                                         excludes='anything outside the launcher (queueing, transfer, upload)'),
    'docker_cache.seconds': dict(unit='s', measures='wall seconds the cache itself cost: begin + prune + finish phases',
                                 excludes="the attempt's own execution (see docker_cache.session_seconds)",
                                 within='docker_cache.session_seconds'),
}


def definition_error(item):
    """The reason a manifest metric definition is invalid, or None."""
    if not isinstance(item, dict) or set(item) != {'name', 'unit', 'measures', 'excludes'}:
        return 'definition must have exactly name, unit, measures, excludes'
    if not isinstance(item['name'], str) or not _NAME.fullmatch(item['name']):
        return 'invalid name'
    for key in ('unit', 'measures', 'excludes'):
        if not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > _TEXT:
            return f'{key} must be a short non-empty string'
    return None


def validate_manifest_metrics(value):
    """Canonical `metrics` list from a handler manifest; WorkloadError 400 when invalid."""
    if not isinstance(value, list) or len(value) > MAX_METRICS:
        raise WorkloadError('handler_manifest_invalid_metrics', 400)
    names = []
    for item in value:
        if definition_error(item) or item['name'] in BUILTIN:
            raise WorkloadError('handler_manifest_invalid_metrics', 400)
        names.append(item['name'])
    if len(set(names)) != len(names) or names != sorted(names):
        raise WorkloadError('handler_manifest_noncanonical_metrics', 400)
    return value


def filter_metrics(metrics, declared=()):
    """(accepted, undeclared_names, misscoped_names). A name with no definition is dropped, a
    non-finite or negative number is dropped as undeclared, and a value above its enclosing
    whole is dropped as mis-scoped. Nothing is zero-filled."""
    known = dict(BUILTIN)
    known.update({item['name']: dict(item) for item in declared if not definition_error(item)})
    accepted, undeclared, misscoped = {}, [], []
    for name, value in sorted((metrics or {}).items())[:MAX_METRICS]:
        if (name not in known or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            undeclared.append(str(name)[:64])
            continue
        accepted[name] = value
    for name in list(accepted):
        whole = known[name].get('within')
        if whole in accepted and accepted[name] > accepted[whole]*_TOLERANCE:
            misscoped.append(name)
            del accepted[name]
    return accepted, undeclared, misscoped
