from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class MqttSecuritySourceTests(unittest.TestCase):
    def test_tls_mode_uses_secure_transport_and_verified_ca(self) -> None:
        header = read("firmware/include/MqttLite.h")
        source = read("firmware/src/MqttLite.cpp")
        self.assertIn("WiFiClientSecure.h", header)
        self.assertRegex(header, r"#if\s+MQTT_TLS[\s\S]*?WiFiClientSecure\s+client_")
        self.assertIn("setCACert(MQTT_CA_CERT)", source)
        self.assertIn("if(!MQTT_CA_CERT[0]) return false;", source)
        self.assertIn("client_.connect(host,port,750)", source)
        self.assertNotIn("setInsecure", header + source)

    def test_secure_build_profile_requires_tls(self) -> None:
        ini = read("firmware/platformio.ini")
        profile = re.search(r"(?ms)^\[env:node1_mqtt_tls_compile\](.*?)(?=^\[env:|\Z)", ini)
        self.assertIsNotNone(profile)
        self.assertRegex(profile.group(1), r"-D\s+MQTT_TLS=1")
        node1 = re.search(r"(?ms)^\[env:node1\](.*?)(?=^\[env:|\Z)", ini)
        self.assertIsNotNone(node1)
        self.assertNotRegex(node1.group(1), r"MQTT_TLS=1")

    def test_firmware_tls_runtime_profile_uses_private_gateway_and_tls(self) -> None:
        ini = read("firmware/platformio.ini")
        profile = re.search(r"(?ms)^\[env:node1_mqtt_tls_runtime\](.*?)(?=^\[env:|\Z)", ini)
        self.assertIsNotNone(profile)
        self.assertRegex(profile.group(1), r'MQTT_HOST=\\"host\.wokwi\.internal\\"')
        self.assertRegex(profile.group(1), r"MQTT_PORT=1884")
        self.assertRegex(profile.group(1), r"MQTT_TLS=1")
        self.assertRegex(profile.group(1), r'MQTT_USER=\\"node1-test\\"')

    def test_tls_runtime_build_requires_password_and_ca(self) -> None:
        build = read("firmware/mqtt_build.py")
        self.assertIn('tls_runtime_target = env.get("PIOENV", "") == "node1_mqtt_tls_runtime"', build)
        self.assertIn("and not tls_compile_target", build)
        self.assertRegex(build, r"if tls_compile_target or tls_runtime_target:")

    def test_runtime_certificate_has_private_gateway_dns_san(self) -> None:
        harness = read("tools/e2e/mqtt_security_harness.py")
        self.assertIn('x509.DNSName("host.wokwi.internal")', harness)
        self.assertIn('x509.DNSName("localhost")', harness)

    def test_firmware_tls_runtime_requires_real_mqtt_and_serial_evidence(self) -> None:
        runtime = read("tools/e2e/firmware_mqtt_tls_runtime.py")
        self.assertIn("class FirmwareTlsBroker", runtime)
        self.assertIn("self.context.wrap_socket(raw, server_side=True)", runtime)
        self.assertIn("mqtt_connected", runtime) if False else None
        self.assertIn('line.startswith("MQTT connected: ")', runtime)
        self.assertIn('"home/{HOME}/device/{DEVICE}/telemetry" in topics', runtime)

    def test_full_runner_builds_and_runs_firmware_tls_runtime(self) -> None:
        runner = read("tools/run-all-tests.ps1")
        self.assertIn("Invoke-LoggedProcess 'firmware-build-mqtt-tls-runtime'", runner)
        self.assertIn("tools/e2e/firmware_mqtt_tls_runtime.py", runner)
        self.assertIn("tlsRuntimePassword = New-RandomMqttPassword", runner)
        self.assertNotIn("this target is compile-only and no firmware-backed TLS broker session is configured", runner)
        runtime_function = re.search(
            r"(?ms)^function Invoke-FirmwareTlsRuntime\(.*?(?=^function |\Z)", runner
        )
        self.assertIsNotNone(runtime_function)
        self.assertIn("Set-NvsRuntimeLogicAnalyzerTrigger", runtime_function.group(0))
        self.assertIn("New-NvsRuntimeWokwiConfig", runtime_function.group(0))

    def test_mosquitto_adapter_uses_loopback_and_ephemeral_credentials(self) -> None:
        runtime = read("tools/e2e/mosquitto_security_runtime.py")
        self.assertIn('sock.bind(("127.0.0.1", 0))', runtime)
        self.assertIn('f"listener {port} 127.0.0.1"', runtime)
        self.assertIn("secrets.token_urlsafe(32)", runtime)
        self.assertIn('args = [str(passwd_exe), "-b"]', runtime)
        self.assertIn('args.extend([str(password_file), username, password])', runtime)
        self.assertIn('"allow_anonymous false"', runtime)
        self.assertIn('"acl_file {quote_config(acl_file)}"', runtime)

    def test_build_requires_external_ca_file_for_tls_profile(self) -> None:
        build = read("firmware/mqtt_build.py")
        self.assertIn("GAS_MQTT_CA_CERT_FILE", build)
        self.assertIn("MQTT_CA_CERT", build)
        self.assertIn("and not tls_compile_target", build)

    def test_acl_denies_node_ack_publish_and_allows_gateway(self) -> None:
        acl = read("tools/e2e/fixtures/mqtt-security.acl")
        self.assertRegex(acl, r"(?ms)^user\s+node1-test$.*?^user\s+gateway-test$")
        node, gateway = re.split(r"(?m)^user\s+gateway-test\s*$", acl, maxsplit=1)
        self.assertNotRegex(node, r"(?m)^topic\s+write\s+home/security-test/device/[^\s]+/ack\s*$")
        self.assertRegex(gateway, r"(?m)^topic\s+write\s+home/security-test/device/\+/ack\s*$")

    def test_policy_harness_checks_acl_read_subscriptions(self) -> None:
        harness = read("tools/e2e/mqtt_security_harness.py")
        self.assertIn("def subscribe(", harness)
        self.assertIn("packet_type == 8", harness)
        self.assertIn('self._allowed(user, "read", topic_filter)', harness)
        self.assertIn("node ACK subscription denied", harness)
        self.assertIn("gateway event wildcard subscription accepted", harness)

    def test_acl_denial_uses_broker_close_not_client_disconnect_packet(self) -> None:
        harness = read("tools/e2e/mqtt_security_harness.py")
        denial_start = harness.index('if not self._allowed(user, "write", topic):')
        denial_end = harness.index("\n                if qos == 1:", denial_start)
        self.assertNotIn("sendall", harness[denial_start:denial_end])
        self.assertIn("except ConnectionError:", harness)

    def test_nvs_removal_still_requires_exact_head_identity(self) -> None:
        source = read("firmware/src/main.cpp")
        self.assertRegex(source, r"if\s*\(eventMatches\(current\.c_str\(\),\s*ack\.bootId,\s*ack\.seq\)\)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
