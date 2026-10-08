"""Small cgroup-v2 execution receipt, sampled before the wrapper exits.

The same `sample` also runs in the worker loop (openspec/changes/measured-resource-
declarations): when the kernel kills the unit the wrapper never writes a receipt, and
the cgroup directory is gone by the time systemd reports `failed` (measured, see
node-py/docs/measured-resources.md), so the loop keeps the last good sample.
A figure that could not be read is ABSENT from the result, never zero.
"""
from pathlib import Path
import os
import re


def sample(path, workspace=None, baseline=None):
    """Resource figures readable from the attempt cgroup `path` (and the workspace
    filesystem delta when both `workspace` and `baseline` are given)."""
    path = Path(path)
    result = {}
    def values(name):
        try:
            with (path/name).open() as stream:
                text = stream.read(4097)
            if len(text) > 4096:
                return {}
            return {key: int(value) for key, value in (line.split() for line in text.splitlines())}
        except (OSError, ValueError):
            return {}
    for filename, key, label in [('memory.events', 'oom_kill', 'oom_kill'),
                                 ('pids.events', 'max', 'pids_max_events'),
                                 ('cpu.stat', 'usage_usec', 'cpu_usage_usec')]:
        data = values(filename)
        if key in data:
            result[label] = data[key]
    for filename, label in [('memory.peak', 'memory_peak_bytes'), ('pids.peak', 'tasks_peak')]:
        try:
            with (path/filename).open() as stream:
                result[label] = int(stream.read(64))
        except (OSError, ValueError):
            pass
    if workspace is not None and baseline is not None:
        used = filesystem_used(workspace)
        if used is not None:
            result['disk_delta_bytes'] = max(0, used-baseline)
    return result


def filesystem_used(workspace):
    """Bytes in use on the filesystem holding `workspace`, or None. One statvfs call,
    never a tree walk. The delta of two readings is the attempt's growth plus whatever
    else wrote to that filesystem meanwhile, so it is an upper bound on the attempt's own."""
    try:
        stat = os.statvfs(workspace)
    except OSError:
        return None
    return (stat.f_blocks-stat.f_bfree)*stat.f_frsize


def resource_usage(workspace=None, baseline=None):
    group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
    path = Path('/sys/fs/cgroup')/group.lstrip('/')
    if path.name == 'supervisor':
        path = path.parent
    if not re.fullmatch(r'harmony-work-[a-f0-9]{16}-[a-f0-9]{32}\.service', path.name):
        return {}
    return sample(path, workspace, baseline)


def unit_evidence(state):
    """Figures a FAILED systemd unit still carries after its cgroup is gone
    (`Result`, `OOMKills`, `MemoryPeak`, `CPUUsageNSec`). `[not set]` and the
    "infinity" sentinel are absent, never numbers."""
    def number(key):
        value = str(state.get(key, ''))
        if not value.isdigit() or int(value) >= 2**64-1:
            return None
        return int(value)
    result = {}
    kills = number('OOMKills')
    if state.get('Result') == 'oom-kill' or kills:
        result['oom_kill'] = max(kills or 0, 1)
    peak, cpu = number('MemoryPeak'), number('CPUUsageNSec')
    if peak is not None:
        result['memory_peak_bytes'] = peak
    if cpu is not None:
        result['cpu_usage_usec'] = cpu//1000
    return result


def merge_evidence(receipt, sampled, unit):
    """One `resources` block with its provenance. A receipt wins and the last loop
    sample fills only what it lacks (`source: receipt`). Without a receipt the unit's
    retained properties and the sample are the evidence (`unit` / `sampled`); with
    neither, `resource_evidence: "none"` is stated and nothing is zero-filled."""
    if receipt is not None:
        merged = dict(receipt)
        for key, value in (sampled or {}).items():
            merged.setdefault(key, value)
        merged['source'] = 'receipt'
        return merged
    merged = dict(sampled or {})
    for key, value in (unit or {}).items():
        merged[key] = max(merged.get(key, 0), value)
    if unit:
        merged['source'] = 'unit'
    elif merged:
        merged['source'] = 'sampled'
    else:
        merged['resource_evidence'] = 'none'
    return merged
