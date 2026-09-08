"""Small cgroup-v2 execution receipt, sampled before the wrapper exits."""
from pathlib import Path
import re


def resource_usage():
    group = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[1]
    path = Path('/sys/fs/cgroup')/group.lstrip('/')
    if path.name == 'supervisor':
        path = path.parent
    if not re.fullmatch(r'harmony-work-[a-f0-9]{16}-[a-f0-9]{32}\.service', path.name):
        return {}
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
    return result
