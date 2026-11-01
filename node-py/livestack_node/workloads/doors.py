"""The worker's `object_relay` setting: one door or an ordered list of doors.

A door is a public edge relay (edge_forward.py) in front of the authority. Moving a door is a
config change here, never a code change: the single-object form `{url, key, parallel?}` stays
valid and is a one-door list named `relay`; the list form is `[{name, url, key, parallel?}, ...]`.
Each door gets its own key, so losing one door's key exposes one door.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .model import WorkloadError

MAX_DOORS = 8
_NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
_FIELDS = {'name', 'url', 'key', 'parallel'}


@dataclass(frozen=True)
class Door:
    name: str
    url: str
    key: str
    parallel: int = 4


def _door(raw, index, legacy):
    if not isinstance(raw, dict) or not {'url', 'key'} <= set(raw) <= _FIELDS:
        raise WorkloadError('object_relay requires url and key, and optionally name and parallel 1..8')
    if legacy and 'name' in raw:
        raise WorkloadError('object_relay name is for the list form; the object form is the door named relay')
    name = raw.get('name', 'relay' if legacy else 'door-%d' % (index+1))
    parallel = raw.get('parallel', 4)
    url, key = raw['url'], raw['key']
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise WorkloadError('object_relay door name must be lowercase letters, digits, - or _')
    if isinstance(parallel, bool) or not isinstance(parallel, int) or not 1 <= parallel <= 8:
        raise WorkloadError('object_relay requires url and key, and optionally parallel 1..8')
    if not isinstance(key, str) or not key:
        raise WorkloadError('object_relay door %s needs a key' % name)
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ('http', 'https') or not parts.hostname or parts.query or parts.fragment \
            or parts.username or parts.password:
        raise WorkloadError('object_relay door %s needs an http(s) url without credentials, query or fragment' % name)
    return Door(name, url.rstrip('/'), key, parallel)


def parse_object_relay(spec) -> list[Door]:
    """`None` (no relay) -> []; the object form -> one door; the list form -> 1..8 distinct doors."""
    if spec is None:
        return []
    if isinstance(spec, list):
        if not 1 <= len(spec) <= MAX_DOORS:
            raise WorkloadError('object_relay list needs 1..%d doors' % MAX_DOORS)
        doors = [_door(raw, i, False) for i, raw in enumerate(spec)]
    else:
        doors = [_door(spec, 0, True)]
    if len({d.name for d in doors}) != len(doors) or len({d.url for d in doors}) != len(doors):
        raise WorkloadError('object_relay doors must have distinct names and urls')
    return doors
