"""Loopback-only MQTT authentication/ACL/TLS policy harness.

This exercises the isolated test policy and Python TLS client. It does not
replace a Mosquitto run or a firmware/Wokwi TLS runtime test.
"""
from __future__ import annotations

import secrets
import socket
import ssl
import struct
import threading
import time
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ROOT = Path(__file__).resolve().parents[2]
ACL_PATH = ROOT / "tools" / "e2e" / "fixtures" / "mqtt-security.acl"
HOME = "security-test"


def mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def mqtt_packet(kind: int, body: bytes) -> bytes:
    length = len(body)
    encoded = bytearray((kind,))
    while True:
        digit = length % 128
        length //= 128
        encoded.append(digit | (0x80 if length else 0))
        if not length:
            return bytes(encoded) + body


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise ConnectionError("peer closed")
        chunks.extend(part)
    return bytes(chunks)


def recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    kind = recv_exact(sock, 1)[0]
    multiplier = 1
    size = 0
    for _ in range(4):
        digit = recv_exact(sock, 1)[0]
        size += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return kind, recv_exact(sock, size)
        multiplier *= 128
    raise ValueError("invalid MQTT remaining-length field")


def read_string(body: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(body):
        raise ValueError("short MQTT string length")
    size = struct.unpack("!H", body[offset:offset + 2])[0]
    offset += 2
    if offset + size > len(body):
        raise ValueError("short MQTT string body")
    return body[offset:offset + size].decode("utf-8"), offset + size


@dataclass(frozen=True)
class Rule:
    user: str
    access: str
    topic_filter: str


def load_acl(path: Path = ACL_PATH) -> list[Rule]:
    rules: list[Rule] = []
    user = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "user" and len(parts) == 2:
            user = parts[1]
        elif parts[0] == "topic" and len(parts) == 3 and user:
            rules.append(Rule(user, parts[1], parts[2]))
        else:
            raise ValueError(f"invalid ACL fixture line: {raw}")
    return rules


def topic_match(topic_filter: str, topic: str) -> bool:
    filt, name = topic_filter.split("/"), topic.split("/")
    for index, part in enumerate(filt):
        if part == "#":
            return True
        if index >= len(name) or (part != "+" and part != name[index]):
            return False
    return len(filt) == len(name)


class PolicyBroker:
    def __init__(self, context: ssl.SSLContext, credentials: dict[str, str], rules: list[Rule]):
        self.context = context
        self.credentials = credentials
        self.rules = rules
        self.stop_event = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.sessions: list[threading.Thread] = []

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=2)

    def _serve(self) -> None:
        while not self.stop_event.is_set():
            try:
                raw, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            session = threading.Thread(target=self._client, args=(raw,), daemon=True)
            self.sessions.append(session)
            session.start()

    def _allowed(self, user: str, access: str, topic: str) -> bool:
        return any(rule.user == user and rule.access == access and topic_match(rule.topic_filter, topic) for rule in self.rules)

    def _client(self, raw: socket.socket) -> None:
        client = None
        try:
            client = self.context.wrap_socket(raw, server_side=True)
            client.settimeout(2)
            kind, body = recv_packet(client)
            if kind != 0x10 or len(body) < 10:
                return
            protocol, offset = read_string(body, 0)
            if protocol != "MQTT" or body[offset] != 4:
                return
            flags = body[offset + 1]
            offset += 4  # level, flags, keepalive
            _client_id, offset = read_string(body, offset)
            if flags & 0x04:
                _will_topic, offset = read_string(body, offset)
                _will_payload, offset = read_string(body, offset)
            user = ""
            password = ""
            if flags & 0x80:
                user, offset = read_string(body, offset)
            if flags & 0x40:
                password, offset = read_string(body, offset)
            if self.credentials.get(user) != password:
                client.sendall(mqtt_packet(0x20, b"\x00\x05"))
                return
            client.sendall(mqtt_packet(0x20, b"\x00\x00"))
            while True:
                kind, body = recv_packet(client)
                packet_type = kind >> 4
                if packet_type == 14:
                    return
                if packet_type == 8:
                    if len(body) < 5:
                        return
                    packet_id = body[:2]
                    offset = 2
                    grants = bytearray()
                    while offset < len(body):
                        topic_filter, offset = read_string(body, offset)
                        if offset >= len(body):
                            return
                        requested_qos = body[offset]
                        offset += 1
                        if requested_qos not in (0, 1, 2) or not self._allowed(user, "read", topic_filter):
                            grants.append(0x80)
                        else:
                            grants.append(min(requested_qos, 1))
                    client.sendall(mqtt_packet(0x90, packet_id + grants))
                    continue
                if packet_type != 3:
                    continue
                topic, offset = read_string(body, 0)
                qos = (kind >> 1) & 3
                packet_id = body[offset:offset + 2] if qos else b""
                if qos:
                    offset += 2
                if not self._allowed(user, "write", topic):
                    # MQTT 3.1.1 has no server-to-client DISCONNECT packet;
                    # deny the operation by closing the connection.
                    return
                if qos == 1:
                    client.sendall(mqtt_packet(0x40, packet_id))
        except (OSError, ssl.SSLError, ConnectionError, ValueError, IndexError):
            return
        finally:
            target = client if client is not None else raw
            try:
                target.close()
            except OSError:
                pass


def issue_certificates(directory: Path) -> tuple[Path, Path, Path, Path]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ephemeral MQTT test CA")])
    ca_cert = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.DNSName("host.wokwi.internal"),
            x509.DNSName("autotest.invalid"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    expired_server_cert = (
        x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=3)).not_valid_after(now - timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.DNSName("host.wokwi.internal"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted test CA")])
    other_cert = (
        x509.CertificateBuilder().subject_name(other_name).issuer_name(other_name)
        .public_key(other_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(other_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    other_path = directory / "other-ca.pem"
    expired_cert_path = directory / "expired-server.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    other_path.write_bytes(other_cert.public_bytes(serialization.Encoding.PEM))
    expired_cert_path.write_bytes(expired_server_cert.public_bytes(serialization.Encoding.PEM))
    return ca_path, cert_path, key_path, other_path


def mqtt_client(
    port: int,
    context: ssl.SSLContext,
    username: str,
    password: str,
    *,
    client_id: str = "security-client",
    keepalive: int = 30,
    will: tuple[str, str, int, bool] | None = None,
) -> tuple[ssl.SSLSocket, int]:
    raw = socket.create_connection(("127.0.0.1", port), timeout=2)
    client = context.wrap_socket(raw, server_hostname="localhost")
    client.settimeout(2)
    flags = 0x02 | 0x80 | 0x40
    if will is not None:
        will_topic, _will_payload, will_qos, will_retain = will
        if will_qos not in (0, 1, 2):
            raise ValueError("MQTT will QoS must be 0, 1, or 2")
        flags |= 0x04 | (will_qos << 3) | (0x20 if will_retain else 0)
    body = mqtt_string("MQTT") + bytes((4, flags, keepalive >> 8, keepalive & 0xFF)) + mqtt_string(client_id)
    if will is not None:
        body += mqtt_string(will_topic) + mqtt_string(will[1])
    body += mqtt_string(username) + mqtt_string(password)
    client.sendall(mqtt_packet(0x10, body))
    kind, response = recv_packet(client)
    if kind != 0x20:
        client.close()
        raise AssertionError(f"unexpected MQTT CONNACK packet: {kind:#x}")
    return client, response[1]


def publish(sock: ssl.SSLSocket, topic: str, payload: str, qos: int = 0,
            retain: bool = False, packet_id: int = 7) -> int:
    if qos not in (0, 1):
        raise ValueError("security harness currently supports MQTT QoS 0 and 1")
    header = 0x30 | (qos << 1) | int(retain)
    body = mqtt_string(topic)
    if qos:
        body += struct.pack("!H", packet_id)
    body += payload.encode("utf-8")
    sock.sendall(mqtt_packet(header, body))
    if qos == 0:
        return 0
    try:
        kind, response = recv_packet(sock)
    except ConnectionError:
        # In this isolated broker, a clean peer close is the expected ACL deny.
        return -1
    if kind != 0x40 or response != struct.pack("!H", packet_id):
        raise AssertionError("expected broker PUBACK or ACL DISCONNECT")
    return 1


def publish_qos1(sock: ssl.SSLSocket, topic: str, payload: str, retain: bool = False) -> int:
    return publish(sock, topic, payload, qos=1, retain=retain)


def subscribe(sock: ssl.SSLSocket, topic_filter: str, qos: int = 1, packet_id: int = 11) -> int:
    body = struct.pack("!H", packet_id) + mqtt_string(topic_filter) + bytes((qos,))
    sock.sendall(mqtt_packet(0x82, body))
    kind, response = recv_packet(sock)
    if kind != 0x90 or response[:2] != struct.pack("!H", packet_id) or len(response) != 3:
        raise AssertionError("expected MQTT SUBACK with one result")
    return response[2]


def receive_publish(sock: ssl.SSLSocket) -> dict[str, object]:
    while True:
        kind, body = recv_packet(sock)
        packet_type = kind >> 4
        if packet_type == 13:  # PINGRESP
            continue
        if packet_type != 3:
            raise AssertionError(f"expected MQTT PUBLISH, received packet type {packet_type}")
        topic, offset = read_string(body, 0)
        qos = (kind >> 1) & 0x03
        if qos not in (0, 1):
            raise AssertionError(f"unexpected delivered MQTT QoS {qos}")
        packet_id = body[offset:offset + 2] if qos else b""
        if qos:
            if len(packet_id) != 2:
                raise AssertionError("truncated MQTT PUBLISH packet identifier")
            offset += 2
            sock.sendall(mqtt_packet(0x40, packet_id))
        return {
            "topic": topic,
            "payload": body[offset:].decode("utf-8"),
            "qos": qos,
            "retain": bool(kind & 0x01),
        }


def disconnect(sock: ssl.SSLSocket) -> None:
    sock.sendall(mqtt_packet(0xE0, b""))
    sock.close()


def run_harness() -> None:
    import tempfile

    credentials = {
        "node1-test": secrets.token_urlsafe(24),
        "gateway-test": secrets.token_urlsafe(24),
    }
    rules = load_acl()
    temp_root = Path(os.environ.get("GAS_E2E_TMPDIR", tempfile.gettempdir()))
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mqtt-security-", dir=temp_root) as temp:
        ca_path, cert_path, key_path, other_ca = issue_certificates(Path(temp))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        broker = PolicyBroker(server_context, credentials, rules)
        broker.start()
        try:
            trusted = ssl.create_default_context(cafile=str(ca_path))
            client, result = mqtt_client(broker.port, trusted, "node1-test", credentials["node1-test"])
            if result != 0:
                raise AssertionError("valid MQTT credentials were rejected")
            print("PASS: trusted CA and matching TLS hostname accepted by local policy broker")
            client.close()

            invalid_auth, result = mqtt_client(broker.port, trusted, "node1-test", "invalid-test-password")
            # MQTT 3.1.1 CONNACK return code 5 means not authorized.
            if result != 5:
                raise AssertionError(f"invalid MQTT credentials got CONNACK {result}, expected 5")
            # The rejected session is closed by broker.
            try:
                invalid_auth.sendall(mqtt_packet(0xE0, b""))
            except OSError:
                pass
            invalid_auth.close()
            print("PASS: invalid MQTT password rejected with CONNACK 0x05")

            anonymous, result = mqtt_client(broker.port, trusted, "", "")
            if result != 5:
                raise AssertionError("anonymous MQTT connection was accepted")
            anonymous.close()
            print("PASS: anonymous MQTT connection rejected")

            node, result = mqtt_client(broker.port, trusted, "node1-test", credentials["node1-test"])
            if result != 0:
                raise AssertionError("node MQTT credentials were rejected")
            own_ack_grant = subscribe(node, f"home/{HOME}/device/node1/ack")
            if own_ack_grant not in (0, 1):
                raise AssertionError("node could not subscribe to its own application ACK")
            other_ack_grant = subscribe(node, f"home/{HOME}/device/node2/ack")
            if other_ack_grant != 0x80:
                raise AssertionError("node was allowed to subscribe to another node's ACK")
            print("PASS: node ACK subscription denied outside its own topic")
            event_ok = publish_qos1(node, f"home/{HOME}/device/node1/event", "{}")
            if event_ok != 1:
                raise AssertionError("node event publish unexpectedly denied")
            print("PASS: node can publish its event topic")
            forged = publish_qos1(node, f"home/{HOME}/device/node1/ack", "runtime-boot:1")
            if forged != -1:
                raise AssertionError("node could publish a forged application ACK")
            print("PASS: ACL denies node publishing forged application ACK")
            node.close()

            gateway, result = mqtt_client(broker.port, trusted, "gateway-test", credentials["gateway-test"])
            if result != 0:
                raise AssertionError("gateway MQTT credentials were rejected")
            event_grant = subscribe(gateway, f"home/{HOME}/device/+/event")
            if event_grant not in (0, 1):
                raise AssertionError("gateway could not subscribe to event wildcard")
            print("PASS: gateway event wildcard subscription accepted")
            ack_ok = publish_qos1(gateway, f"home/{HOME}/device/node1/ack", "runtime-boot:1")
            if ack_ok != 1:
                raise AssertionError("gateway ACK publish unexpectedly denied")
            print("PASS: gateway can publish the application ACK topic")
            gateway.close()

            wrong_ca = ssl.create_default_context(cafile=str(other_ca))
            try:
                client, _result = mqtt_client(broker.port, wrong_ca, "node1-test", credentials["node1-test"])
                client.close()
            except ssl.SSLCertVerificationError:
                print("PASS: untrusted CA rejected by TLS client")
            else:
                raise AssertionError("TLS client accepted an untrusted CA")

            wrong_host = ssl.create_default_context(cafile=str(ca_path))
            raw = socket.create_connection(("127.0.0.1", broker.port), timeout=2)
            try:
                wrong_host.wrap_socket(raw, server_hostname="wrong.example.invalid")
            except ssl.SSLCertVerificationError:
                print("PASS: hostname mismatch rejected by TLS client")
            else:
                raise AssertionError("TLS client accepted a hostname mismatch")
        finally:
            broker.close()
    print("SCOPE: local Python policy broker only; not Mosquitto or ESP32 firmware runtime")


def write_test_ca(output_path: Path) -> None:
    """Write only a disposable public test CA PEM for a compile-only build."""
    import tempfile

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(os.environ.get("GAS_E2E_TMPDIR", tempfile.gettempdir()))
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mqtt-test-ca-", dir=temp_root) as temp:
        ca_path, _server, _key, _other = issue_certificates(Path(temp))
        output_path.write_bytes(ca_path.read_bytes())
    print(f"Wrote ephemeral test CA PEM: {output_path}")


def write_runtime_certificates(directory: Path) -> None:
    """Write disposable test CA and TLS server certificate for local runtimes."""
    directory.mkdir(parents=True, exist_ok=True)
    ca_path, cert_path, key_path, other_path = issue_certificates(directory)
    print("Wrote ephemeral MQTT TLS runtime certificates (credentials omitted).")
    print(f"CA: {ca_path.name}; server certificate: {cert_path.name}; untrusted CA: {other_path.name}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-ca":
        write_test_ca(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--write-runtime-certificates":
        write_runtime_certificates(Path(sys.argv[2]))
    else:
        run_harness()
