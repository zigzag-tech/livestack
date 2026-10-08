"""Tiered retention (openspec/changes/storage-headroom-admission): per-outcome job windows and
per-owner/prefix reference rules. Absent = today's flat behaviour. Plain Python; no value is
echoed in an error. The VALUES live in authority.json; this module is the mechanism."""
import math
from dataclasses import dataclass

HOUR = 3600
MAX_RULES = 64
JOB_KEYS = ('succeeded_seconds', 'failed_seconds', 'cancelled_seconds')
RULE_KEYS = ('owner', 'prefix', 'keep_newest', 'ttl_seconds', 'keep_forever_acknowledged', 'min_age_seconds')
# job.state -> window key. `expired` shares the cancelled tier.
STATE_KEY = {'succeeded': 'succeeded_seconds', 'failed': 'failed_seconds',
             'cancelled': 'cancelled_seconds', 'expired': 'cancelled_seconds'}


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class ReferenceRule:
    owner: str
    prefix: str
    keep_newest: int
    ttl_seconds: float | None   # None only with the explicit acknowledgement

    def matches(self, owner, name):
        return owner == self.owner and name.startswith(self.prefix)


@dataclass(frozen=True)
class RetentionTiers:
    jobs: tuple = ()            # ((window key, seconds), ...)
    references: tuple = ()

    def job_window(self, state, default):
        """Seconds a terminal job of this state is kept; `default` (the flat window) when the
        tier is unset. None means deletion is disabled for it."""
        return dict(self.jobs).get(STATE_KEY.get(state), default)

    def rule_for(self, owner, name):
        found = [r for r in self.references if r.matches(owner, name)]
        return max(found, key=lambda r: len(r.prefix)) if found else None

    @classmethod
    def validate(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError('retention_tiers: Input should be a valid dictionary')
        problems = [f'retention_tiers.{k}: Extra inputs are not permitted' for k in raw if k not in ('jobs', 'references')]
        jobs, rules = {}, []
        section = raw.get('jobs', {})
        if not isinstance(section, dict):
            problems.append('retention_tiers.jobs: Input should be a valid dictionary')
            section = {}
        for key, value in section.items():
            if key not in JOB_KEYS:
                problems.append(f'retention_tiers.jobs.{key}: Extra inputs are not permitted')
            elif not _num(value) or value < HOUR:
                problems.append(f'retention_tiers.jobs.{key}: Input should be a number >= {HOUR}')
            else:
                jobs[key] = float(value)
        listing = raw.get('references', [])
        if not isinstance(listing, list) or len(listing) > MAX_RULES:
            problems.append(f'retention_tiers.references: Input should be a list of at most {MAX_RULES} rules')
            listing = []
        for index, item in enumerate(listing):
            where = f'retention_tiers.references[{index}]'
            if not isinstance(item, dict):
                problems.append(f'{where}: Input should be a valid dictionary')
                continue
            problems += [f'{where}.{k}: Extra inputs are not permitted' for k in item if k not in RULE_KEYS]
            owner, prefix = item.get('owner'), item.get('prefix', '')
            if not isinstance(owner, str) or not owner:
                problems.append(f'{where}.owner: Input should be a non-empty string')
            if not isinstance(prefix, str):
                problems.append(f'{where}.prefix: Input should be a string')
            keep = item.get('keep_newest')
            if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
                problems.append(f'{where}.keep_newest: Input should be an integer >= 1')
            # `min_age_seconds` is the compatible spelling of ttl_seconds.
            ttl_key = 'ttl_seconds' if 'ttl_seconds' in item else 'min_age_seconds'
            if ttl_key not in item:
                problems.append(f'{where}.ttl_seconds: Field required (never-expires needs null plus '
                                'keep_forever_acknowledged)')
                ttl = None
            elif item[ttl_key] is None:
                ttl = None
                if item.get('keep_forever_acknowledged') is not True:
                    problems.append(f'{where}.ttl_seconds: null requires keep_forever_acknowledged true')
            elif not _num(item[ttl_key]) or item[ttl_key] < HOUR:
                problems.append(f'{where}.ttl_seconds: Input should be a number >= {HOUR}')
                ttl = None
            else:
                ttl = float(item[ttl_key])
            if not problems:
                rules.append(ReferenceRule(owner, prefix, keep, ttl))
        if problems:
            raise ValueError('; '.join(problems))
        return cls(jobs=tuple(sorted(jobs.items())), references=tuple(rules))
