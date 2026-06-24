"""Runtime port (port 4) — the only per-service adapter.

A model server implements this to expose its model units to the residence
controller. ``evict`` MUST be per-unit: dropping ``diarize`` must not unload a
co-resident pinned ``asr``.
"""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class ModelRuntime(Protocol):
    def warm(self, unit: str) -> None:
        """Load ``unit`` into VRAM (idempotent; no-op if already resident)."""
        ...

    def evict(self, unit: str) -> None:
        """Free ``unit`` from VRAM (idempotent; no-op if not resident).
        MUST affect only ``unit`` — never co-resident units."""
        ...

    def resident(self) -> List[str]:
        """Currently-resident unit names."""
        ...


class FakeRuntime:
    """In-memory ``ModelRuntime`` for tests and the standalone no-GPU path."""

    def __init__(self) -> None:
        self._resident: set = set()
        self.warm_calls: List[str] = []
        self.evict_calls: List[str] = []

    def warm(self, unit: str) -> None:
        self.warm_calls.append(unit)
        self._resident.add(unit)

    def evict(self, unit: str) -> None:
        self.evict_calls.append(unit)
        self._resident.discard(unit)

    def resident(self) -> List[str]:
        return sorted(self._resident)
