"""Validate the actual Linux Unix-socket byte bound before provider effects."""
from pathlib import Path
import sys


def validate_owner_socket_path(path):
    if sys.platform != 'linux':
        raise ValueError('provider_owner_socket_platform_unsupported')
    if not isinstance(path, (str, Path)):
        raise ValueError('provider_owner_socket_path_required')
    value = str(path)
    try:
        encoded = value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError('provider_owner_socket_utf8_required') from error
    if not value.startswith('/') or b'\0' in encoded or len(encoded) > 107:
        raise ValueError('provider_owner_socket_path_byte_bound')
    return value
