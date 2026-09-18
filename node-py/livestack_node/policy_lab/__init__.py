"""Offline, CPU-only routing policy evaluation tools.

The package intentionally imports no Livestack runtime, GPU, model-serving, or
network modules.  Integration adapters live at the boundary and feed immutable
metadata into this package.
"""

from .smoke import replay_smoke

__all__ = ["replay_smoke"]
