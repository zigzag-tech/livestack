import unittest
from unittest.mock import patch
from livestack_node.provider_socket_path import validate_owner_socket_path
from livestack_node.provider_owner_socket import ProviderOwnerSocket


class SocketPathTests(unittest.TestCase):
    def test_actual_utf8_byte_bound(self):
        self.assertEqual(validate_owner_socket_path('/' + 'a' * 106), '/' + 'a' * 106)
        self.assertEqual(validate_owner_socket_path('/' + 'é' * 53), '/' + 'é' * 53)
        for value in ['/' + 'a' * 107, '/' + 'é' * 54, 'relative', '/a\0b', None, '/\ud800']:
            with self.assertRaises(ValueError):
                validate_owner_socket_path(value)

    def test_owner_refuses_before_private_reads_or_bind(self):
        with patch('livestack_node.provider_owner_socket.os.stat') as observe:
            with self.assertRaisesRegex(ValueError, 'byte_bound'):
                ProviderOwnerSocket('/' + 'a' * 107, '/absent/config', {}, None, None)
            observe.assert_not_called()
