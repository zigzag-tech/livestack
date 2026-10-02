"""Operator-declared route from private Docker containers to native services."""
import ipaddress
import socket
from pathlib import Path

from .model import WorkloadError


def canonical_address(value):
    if value is None:
        raise WorkloadError('native_docker_host_address_missing', 403)
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise WorkloadError('native_docker_host_address_invalid', 403) from error
    if (not isinstance(value, str) or str(address) != value or address.is_loopback or
            address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved):
        raise WorkloadError('native_docker_host_address_invalid', 403)
    return value


def local_address(value):
    address = canonical_address(value)
    # With nonlocal binding disabled, the kernel verifies the declaration names
    # this host. No DNS, source environment, interface scan, or guessed fallback.
    if Path('/proc/sys/net/ipv4/ip_nonlocal_bind').read_text().strip() != '0':
        raise WorkloadError('native_docker_local_address_validation_unavailable', 403)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError as error:
        raise WorkloadError('native_docker_host_address_not_local', 403) from error
    return address
