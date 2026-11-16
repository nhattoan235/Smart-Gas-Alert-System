"""Isolated MQTT-to-SQLite event ACK exercise.

Run with python tools/e2e/mqtt_ack_harness.py. It starts its own minimal
MQTT 3.1.1 broker on loopback, preferring 127.0.0.1:1884 and choosing an
ephemeral loopback port if 1884 is already occupied. The backend uses a
temporary SQLite database. It never connects to the demo broker on port 1883.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOST = "127.0.0.1"
PREFERRED_PORT = 1884
PORT = PREFERRED_PORT
HOME_ID = "e2e-harness"
DEVICE_ID = "node1"
BOOT_ID = "harness-boot"
REPO = Path(__file__).resolve().parents[2]
MAX_PACKET = 1024 * 1024


def mqtt_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def mqtt_packet(kind: int, body: bytes) -> bytes:
    remaining = len(body)
    header = bytearray([kind])
    while True:
        digit = remaining % 128
        remaining //= 128
        header.append(digit | (128 if remaining else 0))
        if not remaining:
            return bytes(header) + body


def recv_exact(sock: socket.socket, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        chunk = sock.recv(length - len(parts))
        if not chunk:
            raise ConnectionError("peer closed MQTT socket")
        parts.extend(chunk)
    return bytes(parts)


def recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = recv_exact(sock, 1)[0]
    multiplier = 1
    remaining = 0
    for _ in range(4):
        digit = recv_exact(sock, 1)[0]
        remaining += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            if remaining > MAX_PACKET:
                raise ValueError("test broker packet exceeded limit")
            return first, recv_exact(sock, remaining)
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


def publish_packet(topic: str, payload: bytes, qos: int = 0, packet_id: int = 1) -> bytes:
    kind = 0x30 | (qos << 1)
    body = mqtt_string(topic)
    if qos:
        body += struct.pack("!H", packet_id)
    return mqtt_packet(kind, body + payload)


def topic_matches(topic_filter: str, topic: str) -> bool:
    filters, topics = topic_filter.split("/"), topic.split("/")
    for index, part in enumerate(filters):
        if part == "#":
            return True
        if index >= len(topics) or (part != "+" and part != topics[index]):
            return False
    return len(filters) == len(topics)


class MiniBroker:
    """Small local-only MQTT subset sufficient for the dashboard worker."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.clients: dict[socket.socket, dict] = {}
        self.packet_id = 0

    def next_packet_id(self) -> int:
        with self.lock:
            self.packet_id = self.packet_id % 65535 + 1
            return self.packet_id

    def send(self, client: dict, packet: bytes) -> None:
        with client["send_lock"]:
            client["socket"].sendall(packet)

    def publish_to_subscribers(self, topic: str, payload: bytes, qos: int) -> None:
        with self.lock:
            recipients = [
                (client, requested_qos)
                for client in self.clients.values()
                for topic_filter, requested_qos in client["subscriptions"]
                if topic_matches(topic_filter, topic)
            ]
        for client, requested_qos in recipients:
            outgoing_qos = min(qos, requested_qos)
            packet_id = self.next_packet_id() if outgoing_qos else 1
            try:
                self.send(client, publish_packet(topic, payload, outgoing_qos, packet_id))
            except OSError:
                try:
                    client["socket"].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle_client(self, conn: socket.socket) -> None:
        client = {"socket": conn, "send_lock": threading.Lock(), "subscriptions": []}
        with self.lock:
            self.clients[conn] = client
        try:
            while True:
                kind, body = recv_packet(conn)
                packet_type = kind >> 4
                if packet_type == 1:  # CONNECT
                    self.send(client, mqtt_packet(0x20, b"\x00\x00"))
                elif packet_type == 8:  # SUBSCRIBE
                    if len(body) < 2:
                        raise ValueError("short SUBSCRIBE")
                    packet_id = body[:2]
                    offset = 2
                    grants = bytearray()
                    subscriptions = []
                    while offset < len(body):
                        if offset + 2 > len(body):
                            raise ValueError("short subscription filter")
                        size = struct.unpack("!H", body[offset:offset + 2])[0]
                        offset += 2
                        topic_filter = body[offset:offset + size].decode("utf-8")
                        offset += size
                        requested_qos = body[offset]
                        offset += 1
                        if requested_qos not in (0, 1):
                            grants.append(0x80)
                        else:
                            subscriptions.append((topic_filter, requested_qos))
                            grants.append(requested_qos)
                    with self.lock:
                        client["subscriptions"].extend(subscriptions)
                    self.send(client, mqtt_packet(0x90, packet_id + bytes(grants)))
                elif packet_type == 3:  # PUBLISH
                    if len(body) < 2:
                        raise ValueError("short PUBLISH")
                    topic_size = struct.unpack("!H", body[:2])[0]
                    offset = 2 + topic_size
                    topic = body[2:offset].decode("utf-8")
                    qos = (kind >> 1) & 0x03
                    packet_id = body[offset:offset + 2] if qos else b""
                    if qos:
                        offset += 2
                    payload = body[offset:]
                    self.publish_to_subscribers(topic, payload, qos)
                    if qos == 1:
                        self.send(client, mqtt_packet(0x40, packet_id))
                elif packet_type == 12:  # PINGREQ
                    self.send(client, mqtt_packet(0xD0, b""))
                elif packet_type in (4, 14):  # PUBACK / DISCONNECT
                    if packet_type == 14:
                        break
                else:
                    # The test exercises only CONNECT/SUBSCRIBE/PUBLISH/QoS1 ACK.
                    continue
        except (ConnectionError, OSError, ValueError, IndexError):
            pass
        finally:
            with self.lock:
                self.clients.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass


def broker_main(ready_file: Path, port: int) -> None:
    broker = MiniBroker()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    listener.bind((HOST, port))
    listener.listen(16)
    listener.settimeout(0.5)
    ready_file.write_text("ready", encoding="ascii")
    print(f"test broker listening at {HOST}:{port}", flush=True)
    try:
        while True:
            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=broker.handle_client, args=(conn,), daemon=True).start()
    finally:
        listener.close()


def backend_main(db_path: Path, port: int) -> None:
    global PORT
    PORT = port
    sys.path.insert(0, str(REPO / "dashboard"))
    import server

    server.DB_PATH = db_path
    server.MQTT_HOST = HOST
    server.MQTT_PORT = PORT
    server.MQTT_USER = ""
    server.MQTT_PASSWORD = ""
    server.HOME_ID = HOME_ID
    server.init_db()
    server.mqtt_worker()


def connect_client(client_id: str) -> socket.socket:
    sock = socket.create_connection((HOST, PORT), timeout=3)
    sock.settimeout(3)
    connect_body = mqtt_string("MQTT") + bytes((4, 0x02, 0, 30)) + mqtt_string(client_id)
    sock.sendall(mqtt_packet(0x10, connect_body))
    kind, body = recv_packet(sock)
    if kind != 0x20 or body != b"\x00\x00":
        sock.close()
        raise AssertionError(f"unexpected MQTT CONNACK: {kind:#x} {body!r}")
    return sock


def subscribe(sock: socket.socket, topic_filter: str, qos: int = 0) -> None:
    packet_id = 1
    body = struct.pack("!H", packet_id) + mqtt_string(topic_filter) + bytes((qos,))
    sock.sendall(mqtt_packet(0x82, body))
    kind, response = recv_packet(sock)
    if kind != 0x90 or response[:2] != struct.pack("!H", packet_id) or response[2:] != bytes((qos,)):
        raise AssertionError(f"unexpected MQTT SUBACK: {kind:#x} {response!r}")


def publish_qos1(sock: socket.socket, topic: str, payload: bytes, packet_id: int) -> None:
    sock.sendall(publish_packet(topic, payload, qos=1, packet_id=packet_id))
    expected = struct.pack("!H", packet_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        kind, body = recv_packet(sock)
        if kind == 0x40 and body == expected:
            return
    raise TimeoutError("test publisher did not receive broker PUBACK")


def receive_publish(sock: socket.socket, timeout: float) -> tuple[str, bytes] | None:
    previous_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        while True:
            kind, body = recv_packet(sock)
            if kind >> 4 != 3:
                continue
            topic_size = struct.unpack("!H", body[:2])[0]
            offset = 2 + topic_size
            topic = body[2:offset].decode("utf-8")
            qos = (kind >> 1) & 0x03
            packet_id = body[offset:offset + 2] if qos else b""
            if qos:
                offset += 2
                if qos == 1:
                    sock.sendall(mqtt_packet(0x40, packet_id))
            return topic, body[offset:]
    except socket.timeout:
        return None
    finally:
        sock.settimeout(previous_timeout)


def read_event(db_path: Path, seq: int) -> list[dict]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT id,device_id,boot_id,seq,kind,payload,server_acked_at FROM messages "
            "WHERE device_id=? AND boot_id=? AND seq=? AND kind='event' ORDER BY id",
            (DEVICE_ID, BOOT_ID, seq),
        ).fetchall()
    finally:
        con.close()
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


def wait_for(predicate, process: subprocess.Popen, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"child exited while waiting for {description}: {process.returncode}")
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {description}")


def backend_log_has(log_path: Path, event: str, seq: int, outcome: str | None = None) -> bool:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event") == event and record.get("seq") == seq:
            if outcome is None or record.get("dedupe_outcome") == outcome:
                return True
    return False


def make_event(seq: int, state: str = "WARNING") -> dict:
    signal = 55.0 if state == "DANGER" else 35.0
    return {
        "schema": 2,
        "kind": "event",
        "home_id": HOME_ID,
        "device_id": DEVICE_ID,
        "boot_id": BOOT_ID,
        "seq": seq,
        "state": state,
        "changed_gas": "LPG",
        "lpg": {"state": state, "signal_pct": signal, "baseline_pct": 3.0,
                "rise_pct": signal - 3.0, "valid": True},
        "co": {"state": "SAFE", "signal_pct": 3.0, "baseline_pct": 3.0,
               "rise_pct": 0.0, "valid": True},
        "temperature_c": 27.0,
        "humidity_pct": 55.0,
        "simulated": True,
    }


def stop_child(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def capture_process_output(stream, chunks: list[str]) -> None:
    try:
        chunks.append(stream.read())
    except (OSError, ValueError):
        pass


def run_harness() -> None:
    global PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_check:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            port_check.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            port_check.bind((HOST, PREFERRED_PORT))
            PORT = PREFERRED_PORT
        except OSError:
            # Keep integration isolated if another local process owns 1884.
            # The child uses exclusive binding too, so a port-selection race
            # fails closed instead of sharing or taking over an existing socket.
            port_check.bind((HOST, 0))
            PORT = int(port_check.getsockname()[1])

    broker_proc = backend_proc = None
    publisher = ack_subscriber = None
    broker_output: list[str] = []
    broker_output_reader = None
    temp_root = Path(os.environ.get("GAS_E2E_TMPDIR", tempfile.gettempdir()))
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mqtt-ack-", dir=temp_root) as temp_name:
        temp = Path(temp_name)
        db_path = temp / "harness.sqlite3"
        ready_file = temp / "broker.ready"
        backend_log = temp / "backend.log"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            broker_proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--broker", str(ready_file), str(PORT)],
                cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=creationflags,
            )
            broker_output_reader = threading.Thread(
                target=capture_process_output, args=(broker_proc.stdout, broker_output), daemon=True,
            )
            broker_output_reader.start()
            wait_for(lambda: ready_file.exists(), broker_proc, 5, "isolated broker startup")

            backend_env = os.environ.copy()
            backend_env.update({
                "GAS_DB": str(db_path),
                "MQTT_HOST": HOST,
                "MQTT_PORT": str(PORT),
                "MQTT_USER": "",
                "GAS_MQTT_PASSWORD": "",
                "MQTT_PASSWORD": "",
                "HOME_ID": HOME_ID,
                "GAS_SYNC_URL": "",
                "GAS_SYNC_TOKEN": "",
            })
            with backend_log.open("w", encoding="utf-8") as output:
                backend_proc = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--backend", str(db_path), str(PORT)],
                    cwd=REPO, env=backend_env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            wait_for(
                lambda: f"MQTT connected: {HOST}:{PORT}" in backend_log.read_text(encoding="utf-8", errors="replace"),
                backend_proc, 10, "backend MQTT subscription",
            )

            ack_topic = f"home/{HOME_ID}/device/{DEVICE_ID}/ack"
            event_topic = f"home/{HOME_ID}/device/{DEVICE_ID}/event"
            ack_subscriber = connect_client("e2e-ack-subscriber")
            subscribe(ack_subscriber, ack_topic, qos=0)
            publisher = connect_client("e2e-device-publisher")

            first_event = make_event(1)
            first_payload = json.dumps(first_event, separators=(",", ":")).encode("utf-8")
            publish_qos1(publisher, event_topic, first_payload, packet_id=1)
            ack = receive_publish(ack_subscriber, 3)
            assert ack == (ack_topic, f"{BOOT_ID}:1".encode("ascii")), f"commit ACK mismatch: {ack!r}"
            saved = read_event(db_path, 1)
            assert len(saved) == 1 and saved[0]["payload"] == first_event, "ACK arrived without one committed matching row"
            assert saved[0]["server_acked_at"] is None, "device commit ACK must not mark cloud sync ACK"
            print("PASS: SQLite commit precedes matching application ACK", flush=True)

            publish_qos1(publisher, event_topic, first_payload, packet_id=2)
            duplicate_ack = receive_publish(ack_subscriber, 3)
            assert duplicate_ack == (ack_topic, f"{BOOT_ID}:1".encode("ascii")), f"duplicate replay ACK missing: {duplicate_ack!r}"
            assert len(read_event(db_path, 1)) == 1, "duplicate event created another SQLite row"
            wait_for(lambda: backend_log_has(backend_log, "sqlite_commit_complete", 1, "duplicate"),
                     backend_proc, 3, "duplicate diagnostic")
            print("PASS: duplicate is idempotent and ACKed again", flush=True)

            conflict = make_event(1)
            conflict["lpg"]["signal_pct"] = 36.0
            conflict_payload = json.dumps(conflict, separators=(",", ":")).encode("utf-8")
            publish_qos1(publisher, event_topic, conflict_payload, packet_id=3)
            wait_for(lambda: backend_log_has(backend_log, "sqlite_commit_complete", 1, "identity_conflict"),
                     backend_proc, 3, "identity conflict diagnostic")
            wait_for(lambda: backend_log_has(backend_log, "application_ack_publish_not_sent", 1, "identity_conflict"),
                     backend_proc, 3, "conflict ACK suppression diagnostic")
            conflict_ack = receive_publish(ack_subscriber, 0.5)
            assert conflict_ack is None, f"conflicting payload was incorrectly application-ACKed: {conflict_ack!r}"
            saved = read_event(db_path, 1)
            assert len(saved) == 1 and saved[0]["payload"] == first_event
            print("PASS: identity conflict is retained as original and receives no app ACK", flush=True)

            # Drop the next QoS0 app ACK by disconnecting the would-be device
            # subscriber. It is not queued/retained; replay must trigger it again.
            ack_subscriber.close()
            ack_subscriber = None
            second_event = make_event(2, "DANGER")
            second_payload = json.dumps(second_event, separators=(",", ":")).encode("utf-8")
            publish_qos1(publisher, event_topic, second_payload, packet_id=4)
            wait_for(lambda: backend_log_has(backend_log, "sqlite_commit_complete", 2, "new"),
                     backend_proc, 3, "new event commit")
            wait_for(lambda: backend_log_has(backend_log, "application_ack_publish_socket_write_complete", 2, "new"),
                     backend_proc, 3, "dropped ACK socket write")
            assert len(read_event(db_path, 2)) == 1, "event with dropped ACK was not committed once"

            ack_subscriber = connect_client("e2e-replay-ack-subscriber")
            subscribe(ack_subscriber, ack_topic, qos=0)
            assert receive_publish(ack_subscriber, 0.25) is None, "non-retained dropped ACK unexpectedly replayed without device retry"
            publish_qos1(publisher, event_topic, second_payload, packet_id=5)
            replay_ack = receive_publish(ack_subscriber, 3)
            assert replay_ack == (ack_topic, f"{BOOT_ID}:2".encode("ascii")), f"replay ACK missing: {replay_ack!r}"
            assert len(read_event(db_path, 2)) == 1, "replay after dropped ACK created duplicate SQLite state"
            wait_for(lambda: backend_log_has(backend_log, "sqlite_commit_complete", 2, "duplicate"),
                     backend_proc, 3, "replay duplicate diagnostic")
            print("PASS: dropped app ACK is recovered by stable-ID replay", flush=True)

            print(f"PASS: isolated MQTT/backend harness completed on loopback port={PORT} (1884 preferred)", flush=True)
        except Exception as exc:
            backend_tail = backend_log.read_text(encoding="utf-8", errors="replace")[-6000:] if backend_log.exists() else ""
            broker_tail = "".join(broker_output)[-2000:]
            raise RuntimeError(f"{exc}\n--- backend log tail ---\n{backend_tail}\n--- broker log tail ---\n{broker_tail}") from exc
        finally:
            for client in (publisher, ack_subscriber):
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass
            stop_child(backend_proc)
            stop_child(broker_proc)
            if broker_output_reader is not None:
                broker_output_reader.join(timeout=3)
            if broker_proc is not None and broker_proc.stdout is not None:
                broker_proc.stdout.close()


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--broker":
        if len(sys.argv) != 4:
            raise SystemExit("broker mode requires ready-file path and port")
        broker_main(Path(sys.argv[2]), int(sys.argv[3]))
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "--backend":
        if len(sys.argv) != 4:
            raise SystemExit("backend mode requires SQLite path and port")
        backend_main(Path(sys.argv[2]), int(sys.argv[3]))
        return 0
    run_harness()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
