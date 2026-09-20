"""Physical CUDA/MLX kinds sharing one logical profile.

Same-kind incompatible descriptors fail at aggregation. Declaration order
does not change eligibility. Unknown memory is not zero bytes and cannot
cold-load.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


CUDA_KIND = "laya_multilingual_cuda_v1"
MLX_KIND = "laya_multilingual_mlx_v1"
LOGICAL_CLASS = "decision"


class KindConflict(ValueError):
    pass


@dataclass
class PhysicalKind:
    kind: str
    backend: str  # cuda | mlx
    profile_id: str
    language: str
    max_len: int
    head_max_len: int
    calibration_hash: str
    resident_bytes: Optional[int]
    peak_bytes: Optional[int]
    load_headroom_bytes: Optional[int]
    healthy: bool = True
    resident: bool = False
    device_id: str = ""
    attributes: Dict[str, object] = field(default_factory=dict)

    def measured(self) -> bool:
        return self.resident_bytes is not None and self.peak_bytes is not None

    def matches_profile(self, profile_id: str, language: Optional[str], max_len: Optional[int]) -> bool:
        if self.profile_id != profile_id:
            return False
        if language is not None and language != self.language:
            return False
        if max_len is not None and max_len != self.max_len:
            return False
        return True


def aggregate_kinds(existing: PhysicalKind, incoming: PhysicalKind) -> PhysicalKind:
    if existing.kind != incoming.kind:
        raise KindConflict("cannot aggregate different kinds")
    # Same-kind incompatible descriptors must fail visibly, never first-seen wins.
    fields = ("backend", "profile_id", "language", "max_len", "head_max_len", "calibration_hash")
    for name in fields:
        if getattr(existing, name) != getattr(incoming, name):
            raise KindConflict(f"same-kind descriptor conflict on {name}")
    return existing


def select_implementation(
    kinds: List[PhysicalKind],
    *,
    profile_id: str,
    language: Optional[str] = None,
    max_len: Optional[int] = None,
    backend: Optional[str] = None,
    warm_only: bool = False,
) -> Optional[PhysicalKind]:
    eligible = [
        k for k in kinds
        if k.healthy
        and k.matches_profile(profile_id, language, max_len)
        and (backend is None or k.backend == backend)
    ]
    if warm_only:
        eligible = [k for k in eligible if k.resident]
    else:
        # Cold load requires a measured envelope. Unknown footprint is ineligible.
        eligible = [k for k in eligible if k.resident or k.measured()]
    if not eligible:
        return None
    # Declaration order must not change eligibility. Prefer resident, then kind name.
    eligible.sort(key=lambda k: (0 if k.resident else 1, k.kind))
    return eligible[0]
