"""Labels a worker derives from a probe instead of a person typing them.

A static label (`labels` in the worker config) says what someone believed the host had. A placement that needs a
tool on the host must match what the host HAS: benchday's native-performance shards were placed on zz-whitby slots
with no Xvfb or weston and died in readiness ("native performance worker missing", 2026-11-21), costing a whole
gate each time. `label_tools` names a label and the executables that make it true:

    "label_tools": {"native_performance": ["Xvfb", "weston", "xdotool"]}

The worker reports `native_performance: "true"` when every tool resolves on its PATH and `"false"` otherwise, so a
selector `{"native_performance": "true"}` places such work only where it can run. A probed label overrides a static
label of the same name (the probe is the evidence). The probe is `shutil.which` only: it runs nothing. Results are
re-probed at most every REPROBE_SECONDS, so installing the tools is picked up without a worker restart.
"""
import shutil
import time

REPROBE_SECONDS = 300
MAX_LABELS = 16
MAX_TOOLS = 32


def validate(value):
    """Config validation: a dict of label -> non-empty list of tool names, bounded."""
    if not isinstance(value, dict) or len(value) > MAX_LABELS:
        raise ValueError(f'label_tools must be an object of at most {MAX_LABELS} labels')
    for label, tools in value.items():
        if not isinstance(label, str) or not label or len(label) > 64:
            raise ValueError('label_tools keys must be label names of 1-64 characters')
        if (not isinstance(tools, list) or not tools or len(tools) > MAX_TOOLS
                or not all(isinstance(t, str) and t and '/' not in t and len(t) <= 128 for t in tools)):
            raise ValueError(f'label_tools[{label!r}] must be 1-{MAX_TOOLS} executable names (no paths)')
    return value


class ProbedLabels:
    def __init__(self, label_tools, which=shutil.which, clock=time.monotonic):
        self.label_tools = dict(validate(label_tools)) if label_tools is not None else {}
        self.which, self.clock = which, clock
        self._at, self._labels, self.missing = None, {}, {}

    def labels(self):
        now = self.clock()
        if self._at is None or now - self._at >= REPROBE_SECONDS:
            labels, missing = {}, {}
            for label, tools in self.label_tools.items():
                absent = [t for t in tools if self.which(t) is None]
                labels[label] = 'false' if absent else 'true'
                if absent:
                    missing[label] = absent
            self._at, self._labels, self.missing = now, labels, missing
        return dict(self._labels)


def merged(static, probed):
    """The report's labels: static ones, with probed values taking precedence."""
    return {**(static or {}), **(probed or {})}
