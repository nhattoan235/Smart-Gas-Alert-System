"""Local MQTT-to-SQLite dashboard. Python standard library only."""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GAS_DB", str(ROOT / "gas_sentinel.sqlite3")))
MQTT_HOST = os.environ.get("MQTT_HOST", "192.168.22.128")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "iotlab")
MQTT_PASSWORD = os.environ.get("GAS_MQTT_PASSWORD", os.environ.get("MQTT_PASSWORD", ""))
HOME_ID = os.environ.get("HOME_ID", "demo-home")
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
NODES = {
    "node1": {"name": "Bo 1 · Bình & van", "zone": "Khu bình gas"},
    "node2": {"name": "Bo 2 · Tuyến ống", "zone": "Dọc tuyến ống"},
    "node3": {"name": "Bo 3 · Khu bếp", "zone": "Khu bếp và sinh hoạt"},
}
SCENARIO_LAYOUT = None
SITE_PATH = Path(os.environ.get("SITE_CONFIG", str(ROOT / "site.json")))
if SITE_PATH.exists():
    try:
        site = json.loads(SITE_PATH.read_text(encoding="utf-8"))
        SCENARIO_LAYOUT = site.get("scenario_layout")
        for device_id, item in site.get("nodes", {}).items():
            if device_id in NODES and isinstance(item, dict):
                distance = item.get("lpg_distance_to_ignition_m")
                if type(distance) in (int, float) and 0 <= distance <= 100:
                    NODES[device_id]["lpg_distance_to_ignition_m"] = float(distance)
                else:
                    NODES[device_id]["lpg_distance_to_ignition_m"] = None
    except (OSError, ValueError):
        print("Invalid site.json: installation distances omitted", flush=True)
STATES = {"WARMUP", "SAFE", "WATCH", "WARNING", "DANGER", "FAULT"}
SEVERITY = {"WARMUP": 0, "SAFE": 1, "WATCH": 2, "WARNING": 3, "FAULT": 4, "DANGER": 5}
ESCALATE_AFTER_S = int(os.environ.get("ESCALATE_AFTER_S", "60"))
MQTT_CONNECTED = False


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA synchronous=FULL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("PRAGMA journal_mode=" + ("WAL" if sqlite3.sqlite_version_info >= (3, 51, 3) else "DELETE"))
        con.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, received_epoch REAL NOT NULL,
          device_id TEXT NOT NULL, boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
          kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
          UNIQUE(device_id, boot_id, seq)
        );
        CREATE INDEX IF NOT EXISTS messages_recent ON messages(device_id, received_epoch DESC);
        CREATE INDEX IF NOT EXISTS messages_kind_time ON messages(kind, received_epoch DESC);
        CREATE TABLE IF NOT EXISTS availability (
          device_id TEXT PRIMARY KEY, online INTEGER NOT NULL, received_epoch REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incidents (
          id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, gas TEXT NOT NULL,
          opened_at REAL NOT NULL, opened_boot_id TEXT NOT NULL, opened_seq INTEGER NOT NULL,
          state TEXT NOT NULL, ack_at REAL, escalated_at REAL, closed_at REAL,
          UNIQUE(device_id, gas, opened_boot_id, opened_seq)
        );
        CREATE INDEX IF NOT EXISTS incidents_open ON incidents(device_id, gas, closed_at);
        """)


def validate(topic: str, data: dict) -> bool:
    bits = topic.split("/")
    if len(bits) != 5 or bits[:2] != ["home", HOME_ID] or bits[2] != "device":
        return False
    if bits[3] not in NODES or bits[4] not in {"telemetry", "event"}:
        return False
    if data.get("schema") != 2 or data.get("home_id") != HOME_ID or data.get("device_id") != bits[3]:
        return False
    if data.get("kind") != bits[4] or data.get("state") not in STATES:
        return False
    if not isinstance(data.get("boot_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", data["boot_id"]):
        return False
    if type(data.get("seq")) is not int or not 0 <= data["seq"] < 2**32:
        return False
    for gas in ("lpg", "co"):
        channel = data.get(gas)
        if not isinstance(channel, dict) or channel.get("state") not in STATES or type(channel.get("valid")) is not bool:
            return False
        for key in ("signal_pct", "baseline_pct", "rise_pct"):
            value = channel.get(key)
            lo, hi = (-100, 100) if key == "rise_pct" else (0, 100)
            if type(value) not in (int, float) or not lo <= value <= hi:
                return False
    expected = max((data["lpg"]["state"], data["co"]["state"]), key=SEVERITY.get)
    if data["state"] != expected or data.get("changed_gas") not in {"NONE", "LPG", "CO", "BOTH"}:
        return False
    if data.get("action", "") not in {"", "MUTE_30S", "ACK_DENIED_DANGER", "INSPECTION_CONFIRMED"}:
        return False
    for key, lo, hi in (("temperature_c", -40, 125), ("humidity_pct", 0, 100)):
        value = data.get(key)
        if type(value) not in (int, float) or not lo <= value <= hi:
            return False
    for key, lo, hi in (("lpg_truth_sim_pct", 0, 100), ("interference_sim_pct", -15, 15), ("environment_bias_sim_pct", -8, 8)):
        if key in data:
            value = data[key]
            if type(value) not in (int, float) or not lo <= value <= hi:
                return False
    for key in ("lpg_leak_truth_sim", "co_hazard_truth_sim", "inspection_required"):
        if key in data and type(data[key]) is not bool:
            return False
    if "lost_events" in data and (type(data["lost_events"]) is not int or not 0 <= data["lost_events"] < 2**32):
        return False
    return type(data.get("simulated")) is bool


def ingest(topic: str, payload: bytes) -> bool:
    if len(payload) > 4096:
        return False
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return False
    bits = topic.split("/")
    now = time.time()
    if isinstance(data, dict) and len(bits) == 5 and bits[:2] == ["home", HOME_ID] and bits[2] == "device" and bits[3] in NODES and bits[4] == "availability":
        if data.get("device_id") != bits[3] or type(data.get("online")) is not bool:
            return False
        with db() as con:
            con.execute("INSERT OR REPLACE INTO availability VALUES (?,?,?)", (bits[3], int(data["online"]), now))
        return True
    if not isinstance(data, dict) or not validate(topic, data):
        return False
    with db() as con:
        cur = con.execute("INSERT OR IGNORE INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload) VALUES(?,?,?,?,?,?,?,?)",
            (datetime.fromtimestamp(now, timezone.utc).isoformat(), now, data["device_id"], data["boot_id"], data["seq"], data["kind"], data["state"], json.dumps(data, separators=(",", ":"))))
        if cur.rowcount != 1:
            return False
        if data["kind"] == "event":
            for gas in ("lpg", "co"):
                if data["changed_gas"] not in (gas.upper(), "BOTH"):
                    continue
                state = data[gas]["state"]
                existing = con.execute("SELECT id FROM incidents WHERE device_id=? AND gas=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
                                       (data["device_id"], gas)).fetchone()
                if state in ("WARNING", "DANGER", "FAULT"):
                    if existing:
                        con.execute("UPDATE incidents SET state=? WHERE id=?", (state, existing["id"]))
                    else:
                        con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state) VALUES(?,?,?,?,?,?)",
                                    (data["device_id"], gas, now, data["boot_id"], data["seq"], state))
                elif state == "SAFE" and existing:
                    con.execute("UPDATE incidents SET state='SAFE',closed_at=? WHERE id=?", (now, existing["id"]))
        return True


def incidents(limit: int = 40) -> list[dict]:
    now = time.time()
    with db() as con:
        con.execute("UPDATE incidents SET escalated_at=? WHERE closed_at IS NULL AND ack_at IS NULL AND escalated_at IS NULL AND opened_at<=?",
                    (now, now - ESCALATE_AFTER_S))
        rows = con.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
    return [dict(row) for row in rows]


def binary_metrics(labels: list[bool], guesses: list[bool]) -> dict:
    tp = sum(actual and guess for actual, guess in zip(labels, guesses))
    fp = sum(not actual and guess for actual, guess in zip(labels, guesses))
    fn = sum(actual and not guess for actual, guess in zip(labels, guesses))
    tn = sum(not actual and not guess for actual, guess in zip(labels, guesses))
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def episode_delays(samples: list[tuple[float, bool, bool]]) -> dict:
    delays = []
    episodes = 0
    start = None
    detected = False
    for at, truth, alarm in samples:
        if truth and start is None:
            episodes += 1
            start, detected = at, False
        if truth and alarm and not detected:
            delays.append(round(max(0, at - start), 1))
            detected = True
        if not truth:
            start = None
    return {"episodes": episodes, "detected_episodes": len(delays),
            "mean_delay_s": round(sum(delays) / len(delays), 1) if delays else None}


def evaluation() -> list[dict]:
    result = []
    with db() as con:
        for device_id in NODES:
            rows = con.execute("SELECT received_epoch,payload FROM messages WHERE device_id=? AND kind='telemetry' AND received_epoch>=? ORDER BY id DESC LIMIT 300",
                               (device_id, time.time() - 1800)).fetchall()
            data = [(row["received_epoch"], json.loads(row["payload"])) for row in reversed(rows)]
            for gas, truth, threshold in (("lpg", "lpg_leak_truth_sim", 12), ("co", "co_hazard_truth_sim", 28)):
                eligible = [(at, item) for at, item in data if type(item.get(truth)) is bool and item.get(gas, {}).get("state") not in ("WARMUP", "FAULT")]
                labels = [item[truth] for _, item in eligible]
                algorithm = [item[gas]["state"] in ("WATCH", "WARNING", "DANGER") for _, item in eligible]
                fixed = [item[gas]["signal_pct"] >= threshold for _, item in eligible]
                result.append({"device_id": device_id, "gas": gas.upper(), "samples": len(eligible),
                               "algorithm": binary_metrics(labels, algorithm), "fixed": binary_metrics(labels, fixed),
                               "episodes": episode_delays([(at, item[truth], alarm) for (at, item), alarm in zip(eligible, algorithm)])})
    return result


def acknowledge_incident(incident_id: int) -> bool:
    with db() as con:
        return con.execute("UPDATE incidents SET ack_at=? WHERE id=? AND closed_at IS NULL AND ack_at IS NULL",
                           (time.time(), incident_id)).rowcount == 1


def committed_event_ack(topic: str, payload: bytes) -> tuple[str, bytes] | None:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("kind") != "event" or not validate(topic, data):
        return None
    with db() as con:
        stored = con.execute("SELECT 1 FROM messages WHERE device_id=? AND boot_id=? AND seq=? AND kind='event'",
                             (data["device_id"], data["boot_id"], data["seq"])).fetchone()
    if not stored:
        return None
    ack_topic = f"home/{HOME_ID}/device/{data['device_id']}/ack"
    return ack_topic, f"{data['boot_id']}:{data['seq']}".encode("ascii")


def snapshot() -> dict:
    now = time.time()
    with db() as con:
        rows = con.execute("""SELECT m.* FROM messages m JOIN
          (SELECT device_id, MAX(id) id FROM messages WHERE kind='telemetry' AND device_id IN ('node1','node2','node3') GROUP BY device_id) x ON m.id=x.id""").fetchall()
        av = {r["device_id"]: r for r in con.execute("SELECT * FROM availability")}
        recent = con.execute("SELECT device_id,received_epoch,payload FROM messages WHERE kind='telemetry' AND received_epoch>=? ORDER BY id DESC", (now - 140,)).fetchall()
    latest = {r["device_id"]: r for r in rows}
    reminder_start = {}
    reminder_last = {}
    reminder_stopped = set()
    for sample in recent:
        device_id = sample["device_id"]
        if device_id in reminder_stopped:
            continue
        data = json.loads(sample["payload"])
        if data.get("valve_open_sim") is not True or data.get("stove_on_sim") is not False:
            reminder_stopped.add(device_id)
            continue
        previous = reminder_last.get(device_id)
        if previous is not None and previous - sample["received_epoch"] > 10:
            reminder_stopped.add(device_id)
            continue
        reminder_last[device_id] = sample["received_epoch"]
        reminder_start[device_id] = sample["received_epoch"]
    nodes = []
    for device_id, meta in NODES.items():
        row = latest.get(device_id)
        age = round(now - row["received_epoch"], 1) if row else None
        online = bool(row and age <= 10 and (device_id not in av or av[device_id]["online"]))
        nodes.append({"device_id": device_id, **meta, "online": online, "age_s": age,
                      "state": json.loads(row["payload"])["state"] if row and online else "OFFLINE",
                      "valve_reminder": bool(online and reminder_start.get(device_id, now) <= now - 120),
                      "latest": json.loads(row["payload"]) if row else None})
    return {"home_id": HOME_ID, "server_time": datetime.now(timezone.utc).isoformat(),
            "broker": {"host": MQTT_HOST, "port": MQTT_PORT, "connected": MQTT_CONNECTED}, "nodes": nodes}


def history(device_id: str, limit: int = 120) -> list[dict]:
    if device_id not in NODES:
        return []
    with db() as con:
        rows = con.execute("SELECT received_at,payload FROM messages WHERE device_id=? AND kind='telemetry' ORDER BY id DESC LIMIT ?", (device_id, min(max(limit, 1), 500))).fetchall()
    return [{"time": r["received_at"], **json.loads(r["payload"])} for r in reversed(rows)]


def events(limit: int = 60) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT received_at,payload FROM messages WHERE kind='event' AND device_id IN ('node1','node2','node3') ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
    return [{"time": r["received_at"], **json.loads(r["payload"])} for r in rows]


def mqtt_string(value: str) -> bytes:
    b = value.encode()
    return struct.pack("!H", len(b)) + b


def mqtt_packet(kind: int, body: bytes) -> bytes:
    length = len(body)
    header = bytearray([kind])
    while True:
        x = length % 128
        length //= 128
        header.append(x | (128 if length else 0))
        if not length:
            break
    return bytes(header) + body


def recv_exact(sock: socket.socket, n: int) -> bytes:
    result = bytearray()
    while len(result) < n:
        part = sock.recv(n - len(result))
        if not part:
            raise ConnectionError("MQTT connection closed")
        result.extend(part)
    return bytes(result)


def recv_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = recv_exact(sock, 1)[0]
    multiplier, length = 1, 0
    for _ in range(4):
        b = recv_exact(sock, 1)[0]
        length += (b & 127) * multiplier
        if not b & 128:
            if length > 65536:
                raise ValueError("MQTT packet too large")
            return first, recv_exact(sock, length)
        multiplier *= 128
    raise ValueError("bad MQTT remaining length")


def mqtt_worker() -> None:
    global MQTT_CONNECTED
    while True:
        try:
            with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=5) as sock:
                sock.settimeout(2)
                client_id = f"gas-dashboard-{os.getpid()}"
                flags = 2 | (128 if MQTT_USER else 0) | (64 if MQTT_PASSWORD else 0)
                body = mqtt_string("MQTT") + bytes([4, flags, 0, 30]) + mqtt_string(client_id)
                if MQTT_USER:
                    body += mqtt_string(MQTT_USER)
                if MQTT_PASSWORD:
                    body += mqtt_string(MQTT_PASSWORD)
                sock.sendall(mqtt_packet(0x10, body))
                if recv_packet(sock) != (0x20, b"\x00\x00"):
                    raise ConnectionError("MQTT CONNACK rejected")
                sock.sendall(mqtt_packet(0x82, b"\x00\x01" + mqtt_string(f"home/{HOME_ID}/device/+/+") + b"\x01"))
                kind, suback = recv_packet(sock)
                if kind != 0x90 or len(suback) != 3 or suback[:2] != b"\x00\x01" or suback[2] not in (0, 1, 2):
                    raise ConnectionError("MQTT subscription rejected")
                MQTT_CONNECTED = True
                print(f"MQTT connected: {MQTT_HOST}:{MQTT_PORT}", flush=True)
                last_ping = time.monotonic()
                while True:
                    try:
                        kind, body = recv_packet(sock)
                    except socket.timeout:
                        if time.monotonic() - last_ping >= 15:
                            sock.sendall(mqtt_packet(0xC0, b""))
                            last_ping = time.monotonic()
                        continue
                    if kind >> 4 != 3:
                        continue
                    topic_len = struct.unpack("!H", body[:2])[0]
                    topic = body[2:2 + topic_len].decode("utf-8")
                    offset = 2 + topic_len
                    qos = (kind >> 1) & 3
                    packet_id = body[offset:offset + 2] if qos else b""
                    if qos:
                        offset += 2
                    try:
                        payload = body[offset:]
                        ingest(topic, payload)
                        ack = committed_event_ack(topic, payload) if topic.endswith("/event") else None
                        if ack:
                            ack_topic, ack_body = ack
                            sock.sendall(mqtt_packet(0x30, mqtt_string(ack_topic) + ack_body))
                    finally:
                        if qos == 1:
                            sock.sendall(mqtt_packet(0x40, packet_id))
        except (OSError, ValueError, ConnectionError) as exc:
            MQTT_CONNECTED = False
            print(f"MQTT waiting: {exc}", flush=True)
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/snapshot":
            return self.send_json(snapshot())
        if parsed.path == "/api/history":
            return self.send_json(history(query.get("device", [""])[0]))
        if parsed.path == "/api/events":
            return self.send_json(events())
        if parsed.path == "/api/incidents":
            return self.send_json(incidents())
        if parsed.path == "/api/layout":
            return self.send_json(SCENARIO_LAYOUT)
        if parsed.path == "/api/evaluation":
            return self.send_json(evaluation())
        name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        assets = {
            "index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css",
            "vendor/bootstrap.min.css": "text/css",
            "vendor/bootstrap.bundle.min.js": "text/javascript",
        }
        if name not in assets:
            self.send_error(404); return
        content = (ROOT / name).read_bytes()
        mime = assets[name]
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/incidents/ack":
            self.send_error(404); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400); return
        if not 0 < length <= 256:
            self.send_error(400); return
        try:
            body = json.loads(self.rfile.read(length))
            incident_id = body.get("id") if isinstance(body, dict) else None
        except (ValueError, UnicodeDecodeError):
            self.send_error(400); return
        if type(incident_id) is not int or incident_id <= 0 or not acknowledge_incident(incident_id):
            self.send_error(400); return
        self.send_json({"ok": True})

    def send_json(self, value: object) -> None:
        content = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=mqtt_worker, daemon=True).start()
    print(f"Dashboard: http://{WEB_HOST}:{WEB_PORT}", flush=True)
    ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler).serve_forever()
