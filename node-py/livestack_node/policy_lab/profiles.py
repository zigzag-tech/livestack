"""Conditional performance profiles and independent keyed sampling."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from typing import Iterable

from .contracts import ContractError


@dataclass(frozen=True, order=True)
class ProfileKey:
    operation: str
    model_revision: str
    runtime_revision: str
    hardware_revision: str
    cache_tier: str
    concurrency: int
    interference: str
    work_units: int

    def __post_init__(self) -> None:
        for field in (
            "operation",
            "model_revision",
            "runtime_revision",
            "hardware_revision",
            "cache_tier",
            "interference",
        ):
            if not getattr(self, field):
                raise ContractError(f"profile {field} must be non-empty")
        if type(self.concurrency) is not int or self.concurrency <= 0:
            raise ContractError("profile concurrency must be positive")
        if type(self.work_units) is not int or self.work_units < 0:
            raise ContractError("profile work_units must be nonnegative")


@dataclass(frozen=True)
class Estimate:
    p50_us: int
    p95_us: int
    lower_us: int
    upper_us: int
    sample_count: int
    measured_at_utc_us: int

    def __post_init__(self) -> None:
        values = (
            self.p50_us,
            self.p95_us,
            self.lower_us,
            self.upper_us,
            self.sample_count,
            self.measured_at_utc_us,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ContractError("profile estimates must be nonnegative integers")
        if not self.lower_us <= self.p50_us <= self.p95_us <= self.upper_us:
            raise ContractError("profile estimate bounds are inconsistent")
        if self.sample_count == 0:
            raise ContractError("measured profile sample_count must be positive")


@dataclass(frozen=True)
class ProfilePoint:
    key: ProfileKey
    estimate: Estimate


@dataclass(frozen=True)
class ProfileLookup:
    status: str
    estimate: Estimate | None
    qualified: bool
    mismatched_dimensions: tuple[str, ...] = ()


IDENTITY_FIELDS = tuple(
    field.name for field in fields(ProfileKey) if field.name != "work_units"
)


def _same_identity(left: ProfileKey, right: ProfileKey) -> bool:
    return all(getattr(left, name) == getattr(right, name) for name in IDENTITY_FIELDS)


def _interpolate(low: Estimate, high: Estimate, numerator: int, denominator: int) -> Estimate:
    def value(name: str) -> int:
        start = getattr(low, name)
        end = getattr(high, name)
        return start + ((end - start) * numerator // denominator)

    return Estimate(
        p50_us=value("p50_us"),
        p95_us=value("p95_us"),
        lower_us=value("lower_us"),
        upper_us=value("upper_us"),
        sample_count=min(low.sample_count, high.sample_count),
        measured_at_utc_us=min(low.measured_at_utc_us, high.measured_at_utc_us),
    )


class ProfilePack:
    def __init__(self, points: Iterable[ProfilePoint]) -> None:
        self._points: dict[ProfileKey, ProfilePoint] = {}
        for point in points:
            if point.key in self._points:
                raise ContractError(f"duplicate profile point: {point.key!r}")
            self._points[point.key] = point

    def lookup(self, query: ProfileKey, *, allow_interpolation: bool = False) -> ProfileLookup:
        exact = self._points.get(query)
        if exact is not None:
            return ProfileLookup("exact", exact.estimate, True)
        compatible = sorted(
            (point for point in self._points.values() if _same_identity(point.key, query)),
            key=lambda point: point.key.work_units,
        )
        if allow_interpolation and compatible:
            lower = [point for point in compatible if point.key.work_units < query.work_units]
            upper = [point for point in compatible if point.key.work_units > query.work_units]
            if lower and upper:
                low = lower[-1]
                high = upper[0]
                estimate = _interpolate(
                    low.estimate,
                    high.estimate,
                    query.work_units - low.key.work_units,
                    high.key.work_units - low.key.work_units,
                )
                return ProfileLookup("interpolated", estimate, True)
            return ProfileLookup("extrapolation_unsupported", None, False)

        mismatches: set[str] = set()
        for point in self._points.values():
            for name in IDENTITY_FIELDS:
                if getattr(point.key, name) != getattr(query, name):
                    mismatches.add(name)
        return ProfileLookup("missing", None, False, tuple(sorted(mismatches)))


def keyed_uniform(seed: int, *key_parts: str) -> float:
    if type(seed) is not int or not all(isinstance(part, str) for part in key_parts):
        raise ContractError("keyed random stream requires integer seed and text keys")
    material = "\x1f".join((str(seed), *key_parts)).encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return integer / 2**64
