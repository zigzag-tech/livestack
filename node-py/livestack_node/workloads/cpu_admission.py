"""worker.json `cpu_admission`: how the worker derives `available.cpu`
(docs/worker-cpu-admission.md). Absent means `loadavg`. Unknown keys fail closed.

Plain Python on purpose: the worker must start on a stock interpreter with no third-party
packages (hand-managed hosts; a missing pydantic crash-looped zz-joe's first roll). The
messages mirror the pydantic ones this replaced. Pydantic stays the authority's dependency
(config.py), never the worker's.
"""
import math
from dataclasses import dataclass


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class CpuAdmission:
    policy: str = 'loadavg'
    # psi: the host counts as stalled (report 0) when /proc/pressure/cpu `full avg60`
    # exceeds this percentage; otherwise the whole capacity less reserve_cpu is offered
    # and placement's reservations decide fit.
    stall_full_avg60_percent: float = 5
    # CPUs held back for work Harmony does not place (agents' own runs).
    reserve_cpu: float = 0
    # Test seam and non-standard mounts; the kernel's file by default.
    psi_path: str = '/proc/pressure/cpu'

    @classmethod
    def validate(cls, raw):
        """Return a CpuAdmission or raise ValueError('<key>: <message>; ...') without values."""
        if not isinstance(raw, dict):
            raise ValueError('<file>: Input should be a valid dictionary')
        problems = []
        known = ('policy', 'stall_full_avg60_percent', 'reserve_cpu', 'psi_path')
        problems += [f'{key}: Extra inputs are not permitted' for key in raw if key not in known]
        values = {}
        if 'policy' in raw:
            if raw['policy'] in ('loadavg', 'psi') and isinstance(raw['policy'], str):
                values['policy'] = raw['policy']
            else:
                problems.append("policy: Input should be 'loadavg' or 'psi'")
        for key, upper in (('stall_full_avg60_percent', 100), ('reserve_cpu', None)):
            if key not in raw:
                continue
            number = raw[key]
            if not _number(number):
                problems.append(f'{key}: Input should be a valid number')
            elif not number >= 0 or math.isnan(number):
                problems.append(f'{key}: Input should be greater than or equal to 0')
            elif upper is not None and number > upper:
                problems.append(f'{key}: Input should be less than or equal to {upper}')
            else:
                values[key] = number
        if 'psi_path' in raw:
            if isinstance(raw['psi_path'], str):
                values['psi_path'] = raw['psi_path']
            else:
                problems.append('psi_path: Input should be a valid string')
        if problems:
            raise ValueError('; '.join(problems))
        return cls(**values)
