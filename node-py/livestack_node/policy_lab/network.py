"""Directed fluid network model with shared bottlenecks and explicit latency."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping

from .contracts import ContractError


@dataclass
class DirectedLink:
    link_id: str
    source: str
    destination: str
    bits_per_second: int
    propagation_us: int
    contention_group: str | None = None
    available: bool = True

    def __post_init__(self) -> None:
        if (
            not self.link_id
            or not self.source
            or not self.destination
            or type(self.bits_per_second) is not int
            or self.bits_per_second <= 0
            or type(self.propagation_us) is not int
            or self.propagation_us < 0
        ):
            raise ContractError("invalid directed link")


@dataclass
class Transfer:
    transfer_id: str
    source: str
    destination: str
    path: tuple[str, ...]
    total_bits: int
    remaining_bits: Fraction
    created_at_us: int
    data_start_us: int
    propagation_us: int
    data_finished_at_us: int | None = None

    @property
    def finished(self) -> bool:
        return self.data_finished_at_us is not None

    @property
    def delivered_at_us(self) -> int | None:
        if self.data_finished_at_us is None:
            return None
        return self.data_finished_at_us + self.propagation_us


class NetworkModel:
    def __init__(
        self,
        links: list[DirectedLink],
        *,
        ingress_caps: Mapping[str, int] | None = None,
        egress_caps: Mapping[str, int] | None = None,
    ) -> None:
        self.links = {link.link_id: link for link in links}
        if len(self.links) != len(links):
            raise ContractError("duplicate directed link id")
        self.ingress_caps = dict(ingress_caps or {})
        self.egress_caps = dict(egress_caps or {})
        if any(type(value) is not int or value <= 0 for value in (*self.ingress_caps.values(), *self.egress_caps.values())):
            raise ContractError("endpoint caps must be positive integers")
        self.now_us = 0
        self._transfers: dict[str, Transfer] = {}
        self._connections: set[tuple[str, str, str]] = set()
        self._cache_locations: dict[str, set[str]] = {}

    def transfer(self, transfer_id: str) -> Transfer:
        return self._transfers[transfer_id]

    def _validate_path(self, source: str, destination: str, path: tuple[str, ...]) -> int:
        if not path:
            raise ContractError("transfer path cannot be empty")
        current = source
        propagation = 0
        for link_id in path:
            link = self.links.get(link_id)
            if link is None or link.source != current or not link.available:
                raise ContractError(f"invalid or unavailable directed path at {link_id}")
            current = link.destination
            propagation += link.propagation_us
        if current != destination:
            raise ContractError("directed path does not reach destination")
        return propagation

    def control_admission_ready(self, *, at_us: int, control_rtt_us: int) -> int:
        if min(at_us, control_rtt_us) < 0:
            raise ContractError("control timing must be nonnegative")
        return at_us + control_rtt_us

    def start_transfer(
        self,
        transfer_id: str,
        *,
        source: str,
        destination: str,
        path: tuple[str, ...],
        byte_count: int,
        at_us: int,
        connection_rtt_us: int = 0,
        route_id: str = "default",
    ) -> Transfer:
        if transfer_id in self._transfers:
            raise ContractError(f"duplicate transfer: {transfer_id}")
        if type(byte_count) is not int or byte_count < 0 or type(connection_rtt_us) is not int or connection_rtt_us < 0:
            raise ContractError("invalid transfer size or connection cost")
        self.advance_to(at_us)
        propagation = self._validate_path(source, destination, path)
        connection_key = (source, destination, route_id)
        setup = 0 if connection_key in self._connections else connection_rtt_us
        self._connections.add(connection_key)
        transfer = Transfer(
            transfer_id=transfer_id,
            source=source,
            destination=destination,
            path=path,
            total_bits=byte_count * 8,
            remaining_bits=Fraction(byte_count * 8),
            created_at_us=at_us,
            data_start_us=at_us + setup,
            propagation_us=propagation,
        )
        self._transfers[transfer_id] = transfer
        return transfer

    def _active(self) -> list[Transfer]:
        return [
            transfer
            for transfer in self._transfers.values()
            if not transfer.finished and transfer.data_start_us <= self.now_us and transfer.remaining_bits > 0
        ]

    def rates_bps(self) -> dict[str, Fraction]:
        active = self._active()
        if not active:
            return {}
        link_users: dict[str, set[str]] = {}
        group_users: dict[str, set[str]] = {}
        source_users: dict[str, set[str]] = {}
        destination_users: dict[str, set[str]] = {}
        for transfer in active:
            source_users.setdefault(transfer.source, set()).add(transfer.transfer_id)
            destination_users.setdefault(transfer.destination, set()).add(transfer.transfer_id)
            for link_id in transfer.path:
                link_users.setdefault(link_id, set()).add(transfer.transfer_id)
                group = self.links[link_id].contention_group
                if group is not None:
                    group_users.setdefault(group, set()).add(transfer.transfer_id)
        rates: dict[str, Fraction] = {}
        for transfer in active:
            bounds: list[Fraction] = []
            for link_id in transfer.path:
                link = self.links[link_id]
                bounds.append(Fraction(link.bits_per_second, len(link_users[link_id])))
                if link.contention_group is not None:
                    group_links = [item for item in self.links.values() if item.contention_group == link.contention_group]
                    group_capacity = min(item.bits_per_second for item in group_links)
                    bounds.append(Fraction(group_capacity, len(group_users[link.contention_group])))
            if transfer.source in self.egress_caps:
                bounds.append(Fraction(self.egress_caps[transfer.source], len(source_users[transfer.source])))
            if transfer.destination in self.ingress_caps:
                bounds.append(Fraction(self.ingress_caps[transfer.destination], len(destination_users[transfer.destination])))
            rates[transfer.transfer_id] = min(bounds)
        return rates

    def advance_to(self, target_us: int) -> None:
        if type(target_us) is not int or target_us < self.now_us:
            raise ContractError("network time cannot move backwards")
        while self.now_us < target_us:
            next_start = min(
                (
                    transfer.data_start_us
                    for transfer in self._transfers.values()
                    if not transfer.finished and transfer.data_start_us > self.now_us
                ),
                default=target_us,
            )
            rates = self.rates_bps()
            if not rates:
                self.now_us = min(target_us, next_start)
                continue
            completion_deltas = {
                transfer_id: (self._transfers[transfer_id].remaining_bits * 1_000_000) / rate
                for transfer_id, rate in rates.items()
            }
            next_completion_delta = min(completion_deltas.values())
            integer_completion = (next_completion_delta.numerator + next_completion_delta.denominator - 1) // next_completion_delta.denominator
            boundary = min(target_us, next_start, self.now_us + integer_completion)
            elapsed = boundary - self.now_us
            for transfer_id, rate in rates.items():
                transfer = self._transfers[transfer_id]
                delivered = rate * elapsed / 1_000_000
                transfer.remaining_bits = max(Fraction(0), transfer.remaining_bits - delivered)
            self.now_us = boundary
            for transfer_id in sorted(rates):
                transfer = self._transfers[transfer_id]
                if transfer.remaining_bits == 0 and transfer.data_finished_at_us is None:
                    transfer.data_finished_at_us = self.now_us

    def update_link_capacity(self, link_id: str, bits_per_second: int, *, at_us: int) -> None:
        self.advance_to(at_us)
        if type(bits_per_second) is not int or bits_per_second <= 0:
            raise ContractError("link capacity must be positive")
        self.links[link_id].bits_per_second = bits_per_second

    @property
    def total_delivered_bytes(self) -> int:
        return sum(transfer.total_bits // 8 for transfer in self._transfers.values() if transfer.finished)

    def record_cache(self, artifact_id: str, region_or_endpoint: str) -> None:
        self._cache_locations.setdefault(artifact_id, set()).add(region_or_endpoint)

    def required_transfer(self, artifact_id: str, *, destination: str) -> bool:
        return destination not in self._cache_locations.get(artifact_id, set())
