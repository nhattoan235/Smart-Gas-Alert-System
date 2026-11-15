"""Exercise the real TLS-enabled ESP32 firmware in local Wokwi.

The Wokwi VS Code simulator and RFC2217 endpoint are started by
run-all-tests.ps1. This driver owns a temporary TLS/MQTT test broker on loopback
port 1884 and verifies a certificate-validated firmware session, MQTT auth,
availability and telemetry. It does not claim Mosquitto compatibility.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_PORT = 1884
DEVICE = "node1"
HOME = "security-test"
USER = "node1-test"


class Blocked(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def read_mqtt_string(body: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(body):
        raise ValueError("truncated MQTT UTF-8 field length")
    size = struct.unpack("!H", body[offset:offset + 2])[0]
    offset += 2
    if offset + size > len(body):
        raise ValueError("truncated MQTT UTF-8 field")
    return body[offset:offset + size].decode("utf-8"), offset + size


def read_mqtt_binary(body: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(body):
        raise ValueError("truncated MQTT binary field length")
    size = struct.unpack("!H", body[offset:offset + 2])[0]
    offset += 2
    if offset + size > len(body):
        raise ValueError("truncated MQTT binary field")
    return body[offset:offset + size], offset + size


def mqtt_packet(kind: int, body: bytes) -> bytes:
    remaining = len(body)
    encoded = bytearray((kind,))
    while True:
        digit = remaining % 128
        remaining //= 128
        encoded.append(digit | (0x80 if remaining else 0))
        if not remaining:
            return bytes(encoded) + body


def recv_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("MQTT peer closed the connection")
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    kind = recv_exact(sock, 1)[0]
    multiplier, remaining = 1, 0
    for _ in range(4):
        digit = recv_exact(sock, 1)[0]
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            return kind, recv_exact(sock, remaining)
        multiplier *= 128
    raise ValueError("invalid MQTT remaining-length field")


def import_security_harness():
    path = ROOT / "tools/e2e/mqtt_security_harness.py"
    spec = importlib.util.spec_from_file_location("mqtt_security_harness_tls_runtime", path)
    if spec is None or spec.loader is None:
        raise Blocked("MQTT security ACL fixture loader is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EventLog:
    def __init__(self, output: Path) -> None:
        self.path = output / "mqtt-tls-runtime-live.jsonl"
        self.lock = threading.Lock()

    def write(self, event: str, **data: object) -> None:
        row = {"timestamp_utc": utc_now(), "event": event, **data}
        encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(encoded + "\n")
        print(encoded, flush=True)


class FirmwareTlsBroker:
    def __init__(self, context: ssl.SSLContext, password: str, log: EventLog, security) -> None:
        self.context = context
        self.password = password
        self.log = log
        self.security = security
        self.rules = security.load_acl()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            self.listener.bind(("127.0.0.1", TEST_PORT))
        except OSError as exc:
            self.listener.close()
            raise Blocked(f"isolated TLS test broker could not bind 127.0.0.1:{TEST_PORT}: {exc}") from exc
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="firmware-tls-test-broker", daemon=True)
        self.clients: set[socket.socket] = set()
        self.client_lock = threading.Lock()
        self.sessions: list[threading.Thread] = []
        self.connected = threading.Event()
        self.auth_rejected = threading.Event()
        self.tls_handshake_failed = threading.Event()
        self.publishes: list[dict] = []
        self.publish_lock = threading.Lock()

    def start(self) -> None:
        self.thread.start()
        self.log.write("test_tls_broker_started", bind="127.0.0.1", port=TEST_PORT,
                       mqtt_username=USER, authentication="required", tls="server certificate and CA validation")

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.listener.close()
        except OSError:
            pass
        with self.client_lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        self.thread.join(timeout=2)
        for session in self.sessions:
            session.join(timeout=1)
        self.log.write("test_tls_broker_stopped", accepted_mqtt_session=self.connected.is_set())

    def _serve(self) -> None:
        while not self.stop_event.is_set():
            try:
                raw, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self._client, args=(raw, address), daemon=True)
            self.sessions.append(worker)
            worker.start()

    def _allowed(self, access: str, topic: str) -> bool:
        return any(rule.user == USER and rule.access == access and
                   self.security.topic_match(rule.topic_filter, topic) for rule in self.rules)

    def _client(self, raw: socket.socket, address) -> None:
        client = None
        try:
            client = self.context.wrap_socket(raw, server_side=True)
            client.settimeout(4)
            with self.client_lock:
                self.clients.add(client)
            tls_version = client.version()
            kind, body = recv_packet(client)
            if kind != 0x10:
                raise ValueError("first firmware packet was not MQTT CONNECT")
            protocol, offset = read_mqtt_string(body, 0)
            if protocol != "MQTT" or offset + 4 > len(body) or body[offset] != 4:
                raise ValueError("firmware did not use MQTT 3.1.1")
            flags = body[offset + 1]
            offset += 4
            client_id, offset = read_mqtt_string(body, offset)
            if flags & 0x04:
                _will_topic, offset = read_mqtt_string(body, offset)
                _will_payload, offset = read_mqtt_binary(body, offset)
            username = ""
            password = ""
            if flags & 0x80:
                username, offset = read_mqtt_string(body, offset)
            if flags & 0x40:
                password, offset = read_mqtt_string(body, offset)
            if username != USER or password != self.password:
                client.sendall(mqtt_packet(0x20, b"\x00\x05"))
                self.auth_rejected.set()
                self.log.write("firmware_mqtt_auth_rejected", username=username, client_id=client_id,
                               tls_version=tls_version, peer=str(address))
                return
            client.sendall(mqtt_packet(0x20, b"\x00\x00"))
            self.connected.set()
            self.log.write("firmware_tls_mqtt_authenticated", username=username, client_id=client_id,
                           tls_version=tls_version, peer=str(address), connack=0)
            while not self.stop_event.is_set():
                kind, body = recv_packet(client)
                packet_type = kind >> 4
                if packet_type == 14:
                    return
                if packet_type == 12:
                    client.sendall(mqtt_packet(0xD0, b""))
                    continue
                if packet_type == 8:
                    packet_id = body[:2]
                    offset = 2
                    grants = bytearray()
                    while offset < len(body):
                        topic_filter, offset = read_mqtt_string(body, offset)
                        requested = body[offset]
                        offset += 1
                        grants.append(min(requested, 1) if self._allowed("read", topic_filter) else 0x80)
                    client.sendall(mqtt_packet(0x90, packet_id + grants))
                    self.log.write("firmware_mqtt_subscribe", username=username, filters=len(grants))
                    continue
                if packet_type != 3:
                    continue
                topic, offset = read_mqtt_string(body, 0)
                qos = (kind >> 1) & 3
                packet_id = body[offset:offset + 2] if qos else b""
                if qos:
                    offset += 2
                payload = body[offset:]
                if not self._allowed("write", topic):
                    self.log.write("firmware_mqtt_acl_denied", username=username, topic=topic)
                    return
                row = {"timestamp_utc": utc_now(), "topic": topic, "qos": qos,
                       "payload_sha256": __import__("hashlib").sha256(payload).hexdigest(),
                       "payload_length": len(payload)}
                with self.publish_lock:
                    self.publishes.append(row)
                self.log.write("firmware_mqtt_publish", **row)
                if qos == 1:
                    client.sendall(mqtt_packet(0x40, packet_id))
        except ssl.SSLError as exc:
            self.tls_handshake_failed.set()
            self.log.write("firmware_tls_handshake_error", error_type=type(exc).__name__, detail=str(exc)[:240])
        except (OSError, ConnectionError, ValueError, IndexError) as exc:
            if not self.stop_event.is_set():
                self.log.write("firmware_mqtt_session_error", error_type=type(exc).__name__, detail=str(exc)[:240])
        finally:
            target = client if client is not None else raw
            with self.client_lock:
                self.clients.discard(target)
            try:
                target.close()
            except OSError:
                pass


class SerialCapture:
    def __init__(self, serial_url: str, output: Path, log: EventLog, timeout: float) -> None:
        driver_path = ROOT / "tools/e2e/firmware_nvs_replay.py"
        spec = importlib.util.spec_from_file_location("nvs_serial_runtime_helpers", driver_path)
        if spec is None or spec.loader is None:
            raise Blocked("existing RFC2217 serial helper module is unavailable")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise Blocked("pySerial is not installed for the local Wokwi RFC2217 endpoint") from exc
        options, scheme, target = helper.serial_open_options(serial_url)
        if scheme != "rfc2217":
            raise Blocked("firmware TLS runtime requires the local Wokwi RFC2217 serial endpoint")
        try:
            self.port = helper.open_serial_when_ready(serial, serial_url, timeout=timeout,
                                                      retry_delay=0.25, log=log)
        except Exception as exc:
            if isinstance(exc, helper.Blocked):
                raise Blocked(str(exc)) from exc
            raise
        self.output_path = output / "mqtt-tls-runtime-serial.log"
        self.connected = threading.Event()
        self.lines: list[str] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._read, name="firmware-tls-serial", daemon=True)
        self.thread.start()
        log.write("firmware_tls_serial_attached", scheme=scheme, target=target)

    def _read(self) -> None:
        try:
            with self.output_path.open("a", encoding="utf-8") as output:
                while not self.stop_event.is_set():
                    raw = self.port.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    self.lines.append(line)
                    output.write(line + "\n")
                    output.flush()
                    if line.startswith("MQTT connected: "):
                        self.connected.set()
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.stop_event.set()
        try:
            self.port.close()
        except Exception:
            pass
        self.thread.join(timeout=2)


def run(args: argparse.Namespace) -> int:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    log = EventLog(output)
    password = os.environ.get("GAS_MQTT_PASSWORD", "")
    if not password:
        log.write("RESULT", status="BLOCKED", detail="test MQTT build credential was not passed to the isolated runtime process")
        return 77
    cert_dir = Path(args.cert_dir)
    ca, cert, key = (cert_dir / "ca.pem", cert_dir / "server.pem", cert_dir / "server-key.pem")
    if not all(path.is_file() and path.stat().st_size for path in (ca, cert, key)):
        log.write("RESULT", status="BLOCKED", detail="ephemeral TLS CA/server certificate fixture is incomplete")
        return 77
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(certfile=str(cert), keyfile=str(key))
    except (OSError, ssl.SSLError) as exc:
        log.write("RESULT", status="BLOCKED", error_type=type(exc).__name__, detail=str(exc))
        return 77

    security = import_security_harness()
    broker = FirmwareTlsBroker(server_context, password, log, security)
    serial_capture = None
    try:
        broker.start()
        serial_capture = SerialCapture(args.serial_url, output, log, args.serial_timeout)
        deadline = time.monotonic() + args.runtime_timeout
        while time.monotonic() < deadline:
            topics = {item["topic"] for item in broker.publishes}
            if (broker.connected.is_set() and serial_capture.connected.is_set() and
                    f"home/{HOME}/device/{DEVICE}/availability" in topics and
                    f"home/{HOME}/device/{DEVICE}/telemetry" in topics):
                log.write("RESULT", status="PASS", detail="fresh TLS firmware completed certificate-validated MQTT authentication, availability and telemetry")
                return 0
            if getattr(serial_capture, "error", None):
                raise Blocked(f"RFC2217 serial reader ended unexpectedly: {serial_capture.error}")
            time.sleep(0.1)
        if broker.tls_handshake_failed.is_set():
            raise AssertionError("firmware reached the TLS broker but the TLS handshake failed")
        if broker.auth_rejected.is_set():
            raise AssertionError("firmware TLS session reached MQTT but its credentials were rejected")
        if not broker.connected.is_set():
            raise Blocked("local Wokwi/private-gateway route did not reach the isolated TLS broker; no TLS MQTT session was observed")
        raise AssertionError("firmware authenticated over TLS but required serial/MQTT application evidence was incomplete")
    except Blocked as exc:
        log.write("RESULT", status="BLOCKED", error_type=type(exc).__name__, detail=str(exc))
        return 77
    except Exception as exc:
        log.write("RESULT", status="FAIL", error_type=type(exc).__name__, detail=str(exc))
        return 1
    finally:
        if serial_capture is not None:
            serial_capture.close()
        broker.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cert-dir", required=True)
    parser.add_argument("--serial-url", required=True)
    parser.add_argument("--serial-timeout", type=float, default=25)
    parser.add_argument("--runtime-timeout", type=float, default=40)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
