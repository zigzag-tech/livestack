"""Closed-form reference calculations independent from the simulator."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractError


def _integer(profile: Mapping[str, Any], key: str, *, positive: bool = False) -> int:
    value = profile.get(key)
    if type(value) is not int or value < (1 if positive else 0):
        raise ContractError(f"{key} must be {'positive' if positive else 'nonnegative'}")
    return value


def load_duration_us(profile: Mapping[str, Any]) -> int:
    byte_count = _integer(profile, "bytes")
    bytes_per_second = _integer(profile, "bytes_per_second", positive=True)
    fixed_us = _integer(profile, "fixed_us")
    return fixed_us + (byte_count * 1_000_000 + bytes_per_second - 1) // bytes_per_second


def transfer_duration_us(profile: Mapping[str, Any]) -> int:
    byte_count = _integer(profile, "bytes")
    bits_per_second = _integer(profile, "bits_per_second", positive=True)
    propagation_us = _integer(profile, "propagation_us")
    return propagation_us + (byte_count * 8 * 1_000_000 + bits_per_second - 1) // bits_per_second


def execution_duration_us(profile: Mapping[str, Any]) -> int:
    work_units = _integer(profile, "work_units")
    units_per_second = _integer(profile, "work_units_per_second", positive=True)
    return (work_units * 1_000_000 + units_per_second - 1) // units_per_second


def peak_memory_bytes(profile: Mapping[str, Any]) -> int:
    return sum(
        _integer(profile, field)
        for field in ("weights_bytes", "staging_bytes", "activation_bytes", "kv_bytes")
    )
