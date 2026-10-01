"""Operator-owned compilation contract; installed tools cannot grant classes.

The policy file is read on every decision so expiry/revocation requires no
authority restart. No caller or worker report supplies its contents. Enrollment
aliases resolve to one physical identity, used both for policy and reservations.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat

from .model import WorkloadError, name


VERSION = 1
MAX_BYTES = 16384
CLASSES = frozenset({'rust', 'flutter', 'image', 'node', 'native'})


@dataclass(frozen=True)
class CompilationDecision:
    host: str
    revision: str
    classes: tuple[str, ...]

    def receipt(self):
        return dict(version=VERSION, host=self.host, policy_revision=self.revision,
                    classes=list(self.classes))


class CompilationPolicy:
    def __init__(self, path, handler_classes):
        self.path = Path(path) if path is not None else None
        if not isinstance(handler_classes, dict) or len(handler_classes) > 64:
            raise WorkloadError('invalid compilation handler inventory')
        self.handler_classes = {}
        for handler, classes in handler_classes.items():
            name(handler, 'compilation handler')
            if (not isinstance(classes, list) or not classes or
                    any(not isinstance(c, str) or c not in CLASSES for c in classes) or
                    len(classes) != len(set(classes))):
                raise WorkloadError('invalid compilation classes')
            self.handler_classes[handler] = tuple(sorted(classes))

    def _read(self, now):
        if self.path is None:
            raise WorkloadError('compilation_policy_missing', 403)
        # Open once, refuse symlinks, inspect the same descriptor that is read.
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or
                        info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022):
                    raise WorkloadError('compilation_policy_untrusted', 403)
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise WorkloadError('compilation_policy_oversized', 403)
            value = json.loads(raw)
        except (OSError, ValueError) as error:
            if isinstance(error, WorkloadError):
                raise
            raise WorkloadError('compilation_policy_unavailable', 503) from error
        if (not isinstance(value, dict) or
                set(value) != {'version', 'revision', 'expires', 'hosts', 'enrollments'} or
                type(value['version']) is not int or value['version'] != VERSION):
            raise WorkloadError('compilation_policy_unsupported', 403)
        name(value['revision'], 'policy revision')
        expiry = value['expires']
        if (isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or
                not math.isfinite(expiry) or expiry <= now):
            raise WorkloadError('compilation_policy_expired', 403)
        hosts, enrollments = value['hosts'], value['enrollments']
        if (not isinstance(hosts, dict) or not 1 <= len(hosts) <= 128 or
                not isinstance(enrollments, dict) or not 1 <= len(enrollments) <= 128):
            raise WorkloadError('compilation_policy_invalid_hosts', 403)
        for host, classes in hosts.items():
            name(host, 'physical host')
            if (not isinstance(classes, list) or len(classes) > len(CLASSES) or
                    any(not isinstance(c, str) or c not in CLASSES for c in classes) or
                    len(set(classes)) != len(classes)):
                raise WorkloadError('compilation_policy_invalid_classes', 403)
        for enrollment, physical in enrollments.items():
            name(enrollment, 'host enrollment')
            if not isinstance(physical, str) or physical not in hosts:
                raise WorkloadError('compilation_policy_unknown_physical_host', 403)
        return value

    def physical_host(self, enrollment, now):
        value = self._read(now)
        physical = value['enrollments'].get(enrollment)
        if physical is None:
            raise WorkloadError('compilation_policy_unknown_enrollment', 403)
        return physical

    def authorize(self, physical, handler, now):
        classes = self.handler_classes.get(handler)
        if classes is None:
            raise WorkloadError('compilation_handler_unclassified', 403)
        value = self._read(now)
        allowed = value['hosts'].get(physical)
        if allowed is None or any(c not in allowed for c in classes):
            raise WorkloadError('compilation_not_admitted: operator host policy', 403)
        return CompilationDecision(physical, value['revision'], classes)

    def required(self, handler):
        return handler in self.handler_classes
