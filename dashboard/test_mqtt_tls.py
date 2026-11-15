from __future__ import annotations

import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class GatewayMqttTlsTests(unittest.TestCase):
    def setUp(self):
        self.original_ca = server.MQTT_TLS_CA_FILE

    def tearDown(self):
        server.MQTT_TLS_CA_FILE = self.original_ca

    def test_local_demo_default_keeps_plain_socket(self):
        server.MQTT_TLS_CA_FILE = ""
        raw = Mock()
        with patch.object(server.socket, "create_connection", return_value=raw) as connect:
            with patch.object(server.ssl, "create_default_context") as tls_context:
                self.assertIs(server.open_mqtt_socket(), raw)
        connect.assert_called_once_with((server.MQTT_HOST, server.MQTT_PORT), timeout=5)
        tls_context.assert_not_called()

    def test_configured_ca_wraps_socket_with_hostname_validation(self):
        server.MQTT_TLS_CA_FILE = "test-ca.pem"
        raw = Mock()
        wrapped = Mock()
        context = Mock()
        context.wrap_socket.return_value = wrapped
        with patch.object(server.socket, "create_connection", return_value=raw):
            with patch.object(server.ssl, "create_default_context", return_value=context) as make_context:
                self.assertIs(server.open_mqtt_socket(), wrapped)
        make_context.assert_called_once_with(cafile="test-ca.pem")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        context.wrap_socket.assert_called_once_with(raw, server_hostname=server.MQTT_HOST)

    def test_certificate_or_hostname_failure_closes_socket_without_plaintext_fallback(self):
        server.MQTT_TLS_CA_FILE = "test-ca.pem"
        raw = Mock()
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("verification failed")
        with patch.object(server.socket, "create_connection", return_value=raw) as connect:
            with patch.object(server.ssl, "create_default_context", return_value=context):
                with self.assertRaises(ssl.SSLCertVerificationError):
                    server.open_mqtt_socket()
        raw.close.assert_called_once()
        connect.assert_called_once()
        context.wrap_socket.assert_called_once_with(raw, server_hostname=server.MQTT_HOST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
