"""Local MQTT-to-SQLite dashboard. Python standard library only."""
from __future__ import annotations

import json
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
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
ALLOW_INSECURE_WEB_BIND = os.environ.get("GAS_WEB_ALLOW_INSECURE_BIND", "0") == "1"
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
ESCALATE_AFTER_S = min(86400, max(1, int(os.environ.get("ESCALATE_AFTER_S", "60"))))
SYNC_URL = os.environ.get("GAS_SYNC_URL", "").strip()
SYNC_TOKEN = os.environ.get("GAS_SYNC_TOKEN", "")
SYNC_API_TOKEN = os.environ.get("GAS_SYNC_API_TOKEN", "")
SYNC_INTERVAL_S = max(1, int(os.environ.get("GAS_SYNC_INTERVAL_S", "5")))
SYNC_BATCH_SIZE = min(100, max(1, int(os.environ.get("GAS_SYNC_BATCH_SIZE", "50"))))
SYNC_RETRY_BASE_S = max(1, int(os.environ.get("GAS_SYNC_RETRY_BASE_S", "2")))
SYNC_RETRY_MAX_S = max(SYNC_RETRY_BASE_S, int(os.environ.get("GAS_SYNC_RETRY_MAX_S", "300")))
SYNC_ALLOW_HTTP = os.environ.get("GAS_SYNC_ALLOW_HTTP", "0") == "1"
NOTIFICATION_PROVIDER = os.environ.get("GAS_NOTIFY_PROVIDER", "mock").strip().lower()
NOTIFICATION_RECIPIENTS = tuple(
    item.strip() for item in os.environ.get("GAS_NOTIFY_RECIPIENTS", "").split(",")
    if item.strip() and len(item.strip()) <= 200 and "\n" not in item and "\r" not in item
)[:50]
NOTIFY_RETRY_BASE_S = max(1, int(os.environ.get("GAS_NOTIFY_RETRY_BASE_S", "5")))
NOTIFY_RETRY_MAX_S = max(NOTIFY_RETRY_BASE_S, int(os.environ.get("GAS_NOTIFY_RETRY_MAX_S", "900")))
NOTIFY_MAX_ATTEMPTS = min(100, max(1, int(os.environ.get("GAS_NOTIFY_MAX_ATTEMPTS", "8"))))
TELEMETRY_RETENTION_DAYS = min(3650, max(1, int(os.environ.get("GAS_TELEMETRY_RETENTION_DAYS", "30"))))
EVENT_RETENTION_DAYS = min(3650, max(1, int(os.environ.get("GAS_EVENT_RETENTION_DAYS", "365"))))
SYNC_RECEIPT_RETENTION_DAYS = min(3650, max(1, int(os.environ.get("GAS_SYNC_RECEIPT_RETENTION_DAYS", "365"))))
NOTIFICATION_RETENTION_DAYS = min(3650, max(1, int(os.environ.get("GAS_NOTIFICATION_RETENTION_DAYS", "365"))))
MQTT_CONNECTED = False


def validate_web_bind(host: str = WEB_HOST, allow_insecure: bool = ALLOW_INSECURE_WEB_BIND) -> None:
    normalized = host.strip().strip("[]").lower()
    try:
        loopback = ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        loopback = normalized == "localhost"
    if not loopback and not allow_insecure:
        raise RuntimeError("WEB_HOST must be loopback-only; public access requires a TLS/auth reverse proxy before setting GAS_WEB_ALLOW_INSECURE_BIND=1")


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


def diagnostic_log(event: str, data: dict, dedupe_outcome: str,
                   error_type: str | None = None, reason: str | None = None) -> None:
    """Write credential-free diagnostics; device NVS queue depth is not observable here."""
    record = {
        "event": event,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "device_id": data["device_id"],
        "boot_id": data["boot_id"],
        "seq": data["seq"],
        "kind": data["kind"],
        "dedupe_outcome": dedupe_outcome,
        "queue_count": None,
    }
    if error_type:
        record["error_type"] = error_type
    if reason:
        record["reason"] = reason
    try:
        print(json.dumps(record, separators=(",", ":"), ensure_ascii=True), flush=True)
    except OSError:
        # Diagnostics must not change ingest or MQTT acknowledgment behavior.
        pass


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        # Secure deletion overwrites legacy payload bytes when compacting old
        # sync receipts below, instead of leaving them in SQLite free pages.
        con.execute("PRAGMA secure_delete=ON")
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
        CREATE TABLE IF NOT EXISTS sync_received (
          source_id TEXT PRIMARY KEY, received_at REAL NOT NULL, home_id TEXT NOT NULL,
          device_id TEXT NOT NULL, boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
          kind TEXT NOT NULL, payload_hash TEXT NOT NULL, message_pruned_at REAL
        );
        CREATE TABLE IF NOT EXISTS sync_state (
          id INTEGER PRIMARY KEY CHECK(id=1), last_attempt_at REAL,
          last_success_at REAL, last_error TEXT
        );
        INSERT OR IGNORE INTO sync_state(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS notification_outbox (
          id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL, recipient TEXT NOT NULL,
          dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0,
          claimed_at REAL, delivered_at REAL, last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_due ON notification_outbox(status, next_attempt_at);
        CREATE TABLE IF NOT EXISTS notification_log (
          id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL, attempted_at REAL NOT NULL,
          provider TEXT NOT NULL, recipient TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
        );
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(messages)")}
        migrations = {
            "server_acked_at": "REAL",
            "sync_attempts": "INTEGER NOT NULL DEFAULT 0",
            "sync_next_attempt_at": "REAL NOT NULL DEFAULT 0",
            "sync_last_error": "TEXT",
        }
        for name, sql_type in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE messages ADD COLUMN {name} {sql_type}")
        sync_columns = {row["name"] for row in con.execute("PRAGMA table_info(sync_received)")}
        if "payload_hash" not in sync_columns:
            con.execute("ALTER TABLE sync_received ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''")
        if "message_pruned_at" not in sync_columns:
            con.execute("ALTER TABLE sync_received ADD COLUMN message_pruned_at REAL")
        if "payload" in sync_columns:
            # Migrate legacy full payload copies to hashes, then clear their
            # contents. The canonical source remains in messages until retention.
            legacy = con.execute("SELECT source_id,payload FROM sync_received WHERE payload_hash='' AND payload<>''").fetchall()
            for row in legacy:
                try:
                    payload_value = json.dumps(json.loads(row["payload"]), separators=(",", ":"), sort_keys=True)
                except (ValueError, TypeError):
                    payload_value = row["payload"]
                digest = hashlib.sha256(payload_value.encode("utf-8")).hexdigest()
                con.execute("UPDATE sync_received SET payload_hash=? WHERE source_id=?", (digest, row["source_id"]))
            con.execute("UPDATE sync_received SET payload='' WHERE payload<>''")
        # Legacy tombstones can outlive a source row already pruned by an older
        # build; start their remaining dedupe horizon at migration time.
        migration_time = time.time()
        con.execute("""UPDATE sync_received SET message_pruned_at=? WHERE message_pruned_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.device_id=sync_received.device_id
            AND m.boot_id=sync_received.boot_id AND m.seq=sync_received.seq AND m.kind=sync_received.kind)""",
                     (migration_time,))
        # A stale claim represents a process that stopped mid-send. It is safe to
        # retry with the same deduplication key after a restart.
        con.execute("UPDATE notification_outbox SET status='pending', claimed_at=NULL, next_attempt_at=0 WHERE status='sending'")


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
    for key in ("valve_open_sim", "stove_on_sim"):
        if key in data and type(data[key]) is not bool:
            return False
    if "lost_events" in data and (type(data["lost_events"]) is not int or not 0 <= data["lost_events"] < 2**32):
        return False
    return type(data.get("simulated")) is bool


def _store_message(con: sqlite3.Connection, data: dict, now: float, received_at: str | None = None) -> bool:
    if received_at is None:
        received_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    cur = con.execute("INSERT OR IGNORE INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload) VALUES(?,?,?,?,?,?,?,?)",
        (received_at, now, data["device_id"], data["boot_id"], data["seq"], data["kind"], data["state"], json.dumps(data, separators=(",", ":"))))
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
                con.execute("UPDATE notification_outbox SET status='cancelled',claimed_at=NULL,last_error='incident closed' WHERE incident_id=? AND status='pending'",
                            (existing["id"],))
    return True


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
    try:
        with db() as con:
            inserted = _store_message(con, data, now)
    except sqlite3.Error as exc:
        diagnostic_log("sqlite_commit_failed", data, "error", type(exc).__name__)
        raise
    # Event diagnostics are emitted only after db() has committed. Routine
    # telemetry remains quiet so logs focus on the device event queue.
    if data["kind"] == "event":
        if inserted:
            dedupe_outcome = "new"
        else:
            with db() as con:
                stored = con.execute("SELECT payload FROM messages WHERE device_id=? AND boot_id=? AND seq=? AND kind=?",
                                     (data["device_id"], data["boot_id"], data["seq"], data["kind"])).fetchone()
            try:
                same_payload = bool(stored) and canonical_payload(json.loads(stored["payload"])) == canonical_payload(data)
            except (ValueError, TypeError):
                same_payload = False
            dedupe_outcome = "duplicate" if same_payload else "identity_conflict"
        diagnostic_log("sqlite_commit_complete", data, dedupe_outcome)
    return inserted


def stable_source_id(data: dict) -> str:
    return json.dumps([data["home_id"], data["device_id"], data["boot_id"], data["seq"], data["kind"]],
                      ensure_ascii=True, separators=(",", ":"))


def canonical_payload(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def payload_digest(data: dict) -> str:
    return hashlib.sha256(canonical_payload(data).encode("utf-8")).hexdigest()


def prune_retention(now: float | None = None) -> dict:
    """Expire low-priority history and terminal metadata without touching active data.

    Receipt tombstones start their configured dedupe horizon when their matching
    message is actually pruned. Terminal notification records and logs share an
    expiry measured from incident closure; pending work/open incidents remain durable.
    """
    now = time.time() if now is None else now
    telemetry_cutoff = now - TELEMETRY_RETENTION_DAYS * 86400
    event_cutoff = now - EVENT_RETENTION_DAYS * 86400
    receipt_cutoff = now - SYNC_RECEIPT_RETENTION_DAYS * 86400
    notification_cutoff = now - NOTIFICATION_RETENTION_DAYS * 86400
    with db() as con:
        con.execute("""UPDATE sync_received SET message_pruned_at=? WHERE message_pruned_at IS NULL
          AND EXISTS (SELECT 1 FROM messages m WHERE m.device_id=sync_received.device_id
            AND m.boot_id=sync_received.boot_id AND m.seq=sync_received.seq AND m.kind=sync_received.kind
            AND m.server_acked_at IS NOT NULL AND (
              (m.kind='telemetry' AND m.state IN ('SAFE','WARMUP') AND m.received_epoch<?)
              OR (m.kind='event' AND m.state IN ('SAFE','WARMUP') AND m.received_epoch<?
                AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.device_id=m.device_id
                  AND i.opened_boot_id=m.boot_id AND i.opened_seq=m.seq AND i.closed_at IS NULL))))""",
                     (now, telemetry_cutoff, event_cutoff))
        telemetry = con.execute("DELETE FROM messages WHERE kind='telemetry' AND state IN ('SAFE','WARMUP') AND server_acked_at IS NOT NULL AND received_epoch<?",
                                (telemetry_cutoff,)).rowcount
        events_deleted = con.execute("""DELETE FROM messages WHERE kind='event' AND state IN ('SAFE','WARMUP')
          AND server_acked_at IS NOT NULL AND received_epoch<?
          AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.device_id=messages.device_id
            AND i.opened_boot_id=messages.boot_id AND i.opened_seq=messages.seq AND i.closed_at IS NULL)""",
                                  (event_cutoff,)).rowcount
        receipts = con.execute("DELETE FROM sync_received WHERE message_pruned_at IS NOT NULL AND message_pruned_at<?",
                             (receipt_cutoff,)).rowcount
        notification_logs = con.execute("""DELETE FROM notification_log WHERE EXISTS
          (SELECT 1 FROM notification_outbox o JOIN incidents i ON i.id=o.incident_id
            WHERE o.id=notification_log.outbox_id AND o.status IN ('simulated','dead','cancelled')
              AND i.closed_at<?)""", (notification_cutoff,)).rowcount
        notification_outbox = con.execute("""DELETE FROM notification_outbox WHERE status IN ('simulated','dead','cancelled')
          AND EXISTS (SELECT 1 FROM incidents i WHERE i.id=notification_outbox.incident_id
            AND i.closed_at<?)""", (notification_cutoff,)).rowcount
    return {"telemetry_deleted": telemetry, "events_deleted": events_deleted,
            "sync_receipts_deleted": receipts, "notification_logs_deleted": notification_logs,
            "notification_outbox_deleted": notification_outbox}


def sync_status() -> dict:
    with db() as con:
        pending = con.execute("SELECT COUNT(*) count, MIN(received_at) oldest FROM messages WHERE server_acked_at IS NULL").fetchone()
        state = con.execute("SELECT last_attempt_at,last_success_at,last_error FROM sync_state WHERE id=1").fetchone()
    return {"configured": bool(SYNC_URL and SYNC_TOKEN), "pending": pending["count"],
            "oldest_pending_at": pending["oldest"], "last_attempt_at": state["last_attempt_at"],
            "last_success_at": state["last_success_at"], "last_error": state["last_error"],
            "dedupe_horizon_days": SYNC_RECEIPT_RETENTION_DAYS}


def _safe_sync_url() -> bool:
    if not SYNC_URL or not SYNC_TOKEN:
        return False
    try:
        parsed = urlparse(SYNC_URL)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.scheme == "https":
            return bool(parsed.hostname)
        return parsed.scheme == "http" and SYNC_ALLOW_HTTP and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def _record_sync_result(rows: list[sqlite3.Row], accepted: set[str], now: float, error: str | None) -> None:
    with db() as con:
        for row in rows:
            data = json.loads(row["payload"])
            source_id = stable_source_id(data)
            attempts = row["sync_attempts"] + 1
            if source_id in accepted:
                con.execute("UPDATE messages SET server_acked_at=?,sync_attempts=?,sync_next_attempt_at=0,sync_last_error=NULL WHERE id=? AND server_acked_at IS NULL",
                            (now, attempts, row["id"]))
            else:
                delay = min(SYNC_RETRY_MAX_S, SYNC_RETRY_BASE_S * (2 ** min(attempts - 1, 12)))
                row_error = error or "server did not acknowledge this source_id"
                con.execute("UPDATE messages SET sync_attempts=?,sync_next_attempt_at=?,sync_last_error=? WHERE id=? AND server_acked_at IS NULL",
                            (attempts, now + delay, row_error[:120], row["id"]))
        con.execute("UPDATE sync_state SET last_attempt_at=?,last_success_at=CASE WHEN ? IS NULL THEN last_success_at ELSE ? END,last_error=? WHERE id=1",
                    (now, error, now, error))


def sync_once() -> int:
    """Send one bounded batch; only explicit server ACK IDs advance local sync state."""
    if not SYNC_URL or not SYNC_TOKEN:
        return 0
    now = time.time()
    if not _safe_sync_url():
        with db() as con:
            con.execute("UPDATE sync_state SET last_attempt_at=?,last_error='unsafe or incomplete sync configuration' WHERE id=1", (now,))
        return 0
    with db() as con:
        rows = con.execute("""SELECT id,received_at,device_id,boot_id,seq,kind,state,payload,sync_attempts
          FROM messages WHERE server_acked_at IS NULL AND sync_next_attempt_at<=?
          ORDER BY CASE WHEN kind='event' THEN 0 WHEN state IN ('WARNING','DANGER','FAULT') THEN 1 ELSE 2 END,
            received_epoch ASC LIMIT ?""", (now, SYNC_BATCH_SIZE)).fetchall()
    if not rows:
        return 0
    messages = []
    source_ids = set()
    for row in rows:
        data = json.loads(row["payload"])
        source_id = stable_source_id(data)
        source_ids.add(source_id)
        messages.append({"source_id": source_id, "received_at": row["received_at"], "payload": data})
    request_body = json.dumps({"home_id": HOME_ID, "messages": messages}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(SYNC_URL, data=request_body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "gas-sentinel-local-gateway/1",
    })
    # urllib's redirect handler forwards regular headers. Mark the bearer header
    # unredirected so a misconfigured endpoint cannot relay it to another host.
    request.add_unredirected_header("Authorization", f"Bearer {SYNC_TOKEN}")
    accepted: set[str] = set()
    error = None
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            content = response.read(65537)
            if len(content) > 65536:
                raise ValueError("sync response too large")
        result = json.loads(content)
        ids = result.get("accepted_source_ids") if isinstance(result, dict) else None
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("invalid sync acknowledgement")
        accepted = source_ids.intersection(ids)
        if len(accepted) != len(source_ids):
            error = "partial server acknowledgement"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Store only the exception class so response bodies, URLs, and credentials
        # cannot accidentally land in the local status API or log.
        error = type(exc).__name__
    _record_sync_result(rows, accepted, now, error)
    return len(accepted)


def sync_worker() -> None:
    while True:
        try:
            sync_once()
        except (OSError, sqlite3.Error, ValueError) as exc:
            with db() as con:
                con.execute("UPDATE sync_state SET last_attempt_at=?,last_error=? WHERE id=1", (time.time(), type(exc).__name__))
        time.sleep(SYNC_INTERVAL_S)


def receive_sync_batch(body: dict) -> list[str]:
    """Validate and durably store an idempotent server-side sync batch."""
    if not isinstance(body, dict) or body.get("home_id") != HOME_ID:
        raise ValueError("invalid sync home")
    items = body.get("messages")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("invalid sync batch size")
    prepared = []
    seen_source_ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            raise ValueError("invalid sync message")
        data = item["payload"]
        source_id = item.get("source_id")
        received_at = item.get("received_at")
        topic = f"home/{HOME_ID}/device/{data.get('device_id', '')}/{data.get('kind', '')}"
        if not validate(topic, data) or not isinstance(source_id, str) or source_id != stable_source_id(data):
            raise ValueError("invalid sync message identity")
        if len(json.dumps(data, separators=(",", ":")).encode("utf-8")) > 4096:
            raise ValueError("sync message too large")
        if source_id in seen_source_ids:
            raise ValueError("duplicate sync source ID")
        seen_source_ids.add(source_id)
        if not isinstance(received_at, str) or len(received_at) > 80:
            raise ValueError("invalid sync timestamp")
        try:
            received_dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if received_dt.tzinfo is None:
                received_dt = received_dt.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError("invalid sync timestamp") from exc
        prepared.append((source_id, received_dt.timestamp(), received_at, data, topic))

    accepted = []
    now = time.time()
    with db() as con:
        # Check source IDs and payload conflicts before writing any part of the batch.
        for source_id, _, _, data, _ in prepared:
            digest = payload_digest(data)
            existing = con.execute("SELECT payload_hash FROM sync_received WHERE source_id=?", (source_id,)).fetchone()
            if existing and existing["payload_hash"] != digest:
                raise RuntimeError("sync source ID conflict")
        for source_id, received_epoch, received_at, data, topic in prepared:
            canonical = json.dumps(data, separators=(",", ":"), sort_keys=True)
            digest = payload_digest(data)
            receipt = con.execute("SELECT payload_hash FROM sync_received WHERE source_id=?", (source_id,)).fetchone()
            if receipt:
                accepted.append(source_id)
                continue
            existing = con.execute("SELECT payload,kind FROM messages WHERE device_id=? AND boot_id=? AND seq=?",
                                   (data["device_id"], data["boot_id"], data["seq"])).fetchone()
            if existing:
                if existing["kind"] != data["kind"] or json.dumps(json.loads(existing["payload"]), separators=(",", ":"), sort_keys=True) != canonical:
                    raise RuntimeError("local message identity conflict")
            else:
                if not _store_message(con, data, received_epoch, received_at):
                    raise RuntimeError("could not commit synchronized message")
            con.execute("INSERT OR IGNORE INTO sync_received(source_id,received_at,home_id,device_id,boot_id,seq,kind,payload_hash) VALUES(?,?,?,?,?,?,?,?)",
                        (source_id, now, HOME_ID, data["device_id"], data["boot_id"], data["seq"], data["kind"], digest))
            # The receiver has committed this source ID, so do not forward the
            # same row back out if this server also has an outbound sync URL.
            con.execute("UPDATE messages SET server_acked_at=COALESCE(server_acked_at,?) WHERE device_id=? AND boot_id=? AND seq=?",
                        (now, data["device_id"], data["boot_id"], data["seq"]))
            accepted.append(source_id)
    return accepted


def queue_expired_notifications(now: float | None = None) -> int:
    now = time.time() if now is None else now
    recipients = tuple(dict.fromkeys(NOTIFICATION_RECIPIENTS))
    deadline = now - ESCALATE_AFTER_S
    queued = 0
    with db() as con:
        due = con.execute("SELECT * FROM incidents WHERE closed_at IS NULL AND ack_at IS NULL AND escalated_at IS NULL AND opened_at<=?",
                          (deadline,)).fetchall()
        for incident in due:
            con.execute("UPDATE incidents SET escalated_at=? WHERE id=? AND escalated_at IS NULL AND ack_at IS NULL AND closed_at IS NULL",
                        (now, incident["id"]))
            if not recipients:
                continue
            body = {"incident_id": incident["id"], "device_id": incident["device_id"],
                    "gas": incident["gas"].upper(), "state": incident["state"],
                    "opened_at": incident["opened_at"],
                    "acknowledgement_deadline": incident["opened_at"] + ESCALATE_AFTER_S,
                    "acknowledgement_expired": True, "simulated": True}
            for recipient in recipients:
                dedupe = hashlib.sha256(f"{incident['id']}\0{recipient}".encode("utf-8")).hexdigest()
                cur = con.execute("INSERT OR IGNORE INTO notification_outbox(incident_id,recipient,dedupe_key,payload,status,attempts,created_at,next_attempt_at) VALUES(?,?,?,?, 'pending',0,?,?)",
                                  (incident["id"], recipient, dedupe, json.dumps(body, separators=(",", ":")), now, now))
                queued += cur.rowcount
    return queued


def incidents(limit: int = 40) -> list[dict]:
    now = time.time()
    queue_expired_notifications(now)
    with db() as con:
        rows = con.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["ack_deadline"] = item["opened_at"] + ESCALATE_AFTER_S
        result.append(item)
    return result


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
    """Compare detector decisions only with valid per-gas telemetry samples.

    Schema 2 carries independent `valid` booleans in the `lpg` and `co`
    channel objects. An invalid channel is excluded from its metrics and episode
    timing, while the other gas channel from that same telemetry record remains
    eligible when its own validity flag is true.
    """
    result = []
    with db() as con:
        for device_id in NODES:
            rows = con.execute("SELECT received_epoch,payload FROM messages WHERE device_id=? AND kind='telemetry' AND received_epoch>=? ORDER BY id DESC LIMIT 300",
                               (device_id, time.time() - 1800)).fetchall()
            data = [(row["received_epoch"], json.loads(row["payload"])) for row in reversed(rows)]
            for gas, truth, threshold in (("lpg", "lpg_leak_truth_sim", 12), ("co", "co_hazard_truth_sim", 28)):
                labeled = [(at, item) for at, item in data
                           if type(item.get(truth)) is bool and isinstance(item.get(gas), dict)
                           and item[gas].get("state") not in ("WARMUP", "FAULT")]
                excluded_invalid = sum(item[gas].get("valid") is not True for _, item in labeled)
                eligible = [(at, item) for at, item in labeled if item[gas].get("valid") is True]
                labels = [item[truth] for _, item in eligible]
                algorithm = [item[gas]["state"] in ("WATCH", "WARNING", "DANGER") for _, item in eligible]
                fixed = [item[gas]["signal_pct"] >= threshold for _, item in eligible]
                result.append({"device_id": device_id, "gas": gas.upper(), "samples": len(eligible),
                               "excluded_invalid_samples": excluded_invalid,
                               "algorithm": binary_metrics(labels, algorithm), "fixed": binary_metrics(labels, fixed),
                               "episodes": episode_delays([(at, item[truth], alarm) for (at, item), alarm in zip(eligible, algorithm)])})
    return result


def acknowledge_incident(incident_id: int) -> bool:
    with db() as con:
        updated = con.execute("UPDATE incidents SET ack_at=? WHERE id=? AND closed_at IS NULL AND ack_at IS NULL",
                              (time.time(), incident_id)).rowcount == 1
        if updated:
            con.execute("UPDATE notification_outbox SET status='cancelled',claimed_at=NULL,last_error='incident acknowledged' WHERE incident_id=? AND status='pending'",
                        (incident_id,))
        return updated


class MockNotificationProvider:
    """Local audit adapter; it never sends an email, SMS, or push message."""
    name = "mock"

    def send(self, recipient: str, payload: dict, dedupe_key: str) -> dict:
        return {"provider": self.name, "simulated": True, "receipt": dedupe_key[:16]}


def process_notification_outbox(provider=None, now: float | None = None, limit: int = 20) -> int:
    now = time.time() if now is None else now
    queue_expired_notifications(now)
    if NOTIFICATION_PROVIDER != "mock":
        return 0
    provider = provider or MockNotificationProvider()
    if getattr(provider, "name", "mock") != "mock":
        return 0
    with db() as con:
        rows = con.execute("SELECT * FROM notification_outbox WHERE status='pending' AND next_attempt_at<=? AND attempts<? ORDER BY id LIMIT ?",
                           (now, NOTIFY_MAX_ATTEMPTS, min(max(limit, 1), 100))).fetchall()
    processed = 0
    for row in rows:
        with db() as con:
            claimed = con.execute("UPDATE notification_outbox SET status='sending',attempts=attempts+1,claimed_at=? WHERE id=? AND status='pending' AND next_attempt_at<=?",
                                  (now, row["id"], now)).rowcount
            if claimed != 1:
                continue
        processed += 1
        attempt = row["attempts"] + 1
        try:
            provider.send(row["recipient"], json.loads(row["payload"]), row["dedupe_key"])
        except Exception as exc:
            failed_at = time.time()
            exhausted = attempt >= NOTIFY_MAX_ATTEMPTS
            delay = min(NOTIFY_RETRY_MAX_S, NOTIFY_RETRY_BASE_S * (2 ** min(attempt - 1, 12)))
            detail = type(exc).__name__
            with db() as con:
                con.execute("UPDATE notification_outbox SET status=?,next_attempt_at=?,claimed_at=NULL,last_error=? WHERE id=?",
                            ("dead" if exhausted else "pending", failed_at if exhausted else failed_at + delay, detail, row["id"]))
                con.execute("INSERT INTO notification_log(outbox_id,attempted_at,provider,recipient,status,detail) VALUES(?,?,?,?,?,?)",
                            (row["id"], failed_at, "mock", row["recipient"], "failed", detail))
        else:
            delivered_at = time.time()
            with db() as con:
                con.execute("UPDATE notification_outbox SET status='simulated',delivered_at=?,claimed_at=NULL,last_error=NULL WHERE id=?",
                            (delivered_at, row["id"]))
                con.execute("INSERT INTO notification_log(outbox_id,attempted_at,provider,recipient,status,detail) VALUES(?,?,?,?,?,?)",
                            (row["id"], delivered_at, "mock", row["recipient"], "simulated", "mock provider logged; no external message sent"))
    return processed


def notification_status() -> dict:
    with db() as con:
        counts = {row["status"]: row["count"] for row in con.execute("SELECT status,COUNT(*) count FROM notification_outbox GROUP BY status")}
        logs = con.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
    return {"provider": "mock" if NOTIFICATION_PROVIDER == "mock" else "disabled",
            "simulated": NOTIFICATION_PROVIDER == "mock", "configured_recipients": len(set(NOTIFICATION_RECIPIENTS)),
            "pending": counts.get("pending", 0) + counts.get("sending", 0),
            "simulated_deliveries": counts.get("simulated", 0), "failed_or_dead": counts.get("dead", 0),
            "attempt_log_entries": logs, "acknowledgement_expiry_s": ESCALATE_AFTER_S}


def notification_log(limit: int = 50) -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT id,outbox_id,attempted_at,provider,status,detail,recipient FROM notification_log ORDER BY id DESC LIMIT ?",
                           (min(max(limit, 1), 200),)).fetchall()
    return [{key: row[key] for key in ("id", "outbox_id", "attempted_at", "provider", "status", "detail")}
            | {"recipient_ref": hashlib.sha256(row["recipient"].encode("utf-8")).hexdigest()[:10]}
            for row in rows]


def notification_worker() -> None:
    while True:
        try:
            process_notification_outbox()
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f"Notification worker waiting: {type(exc).__name__}", flush=True)
        time.sleep(2)


def retention_worker() -> None:
    while True:
        try:
            prune_retention()
        except (OSError, sqlite3.Error) as exc:
            print(f"Retention maintenance waiting: {type(exc).__name__}", flush=True)
        time.sleep(3600)


def _committed_event_ack_result(topic: str, payload: bytes) -> tuple[tuple[str, bytes] | None, dict | None, str]:
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None, None, "invalid"
    if not isinstance(data, dict) or data.get("kind") != "event" or not validate(topic, data):
        return None, None, "invalid"
    with db() as con:
        stored = con.execute("SELECT payload FROM messages WHERE device_id=? AND boot_id=? AND seq=? AND kind='event'",
                             (data["device_id"], data["boot_id"], data["seq"])).fetchone()
    if not stored:
        return None, data, "not_committed"
    try:
        stored_payload = json.loads(stored["payload"])
    except (ValueError, TypeError):
        return None, data, "identity_conflict"
    if canonical_payload(stored_payload) != canonical_payload(data):
        return None, data, "identity_conflict"
    ack_topic = f"home/{HOME_ID}/device/{data['device_id']}/ack"
    return (ack_topic, f"{data['boot_id']}:{data['seq']}".encode("ascii")), data, "matching"


def committed_event_ack(topic: str, payload: bytes) -> tuple[str, bytes] | None:
    ack, _data, _status = _committed_event_ack_result(topic, payload)
    return ack


def publish_committed_event_ack(sock: socket.socket, topic: str, payload: bytes,
                                newly_inserted: bool) -> bool:
    """Publish an app ACK only after rechecking the exact committed payload.

    A completed sendall() means the local socket accepted the full packet; it
    does not prove broker delivery or device receipt.
    """
    ack, data, status = _committed_event_ack_result(topic, payload)
    if ack is None:
        if status == "identity_conflict" and data is not None:
            diagnostic_log("application_ack_publish_not_sent", data, "identity_conflict",
                           reason="stored_payload_mismatch")
        return False
    assert data is not None
    outcome = "new" if newly_inserted else "duplicate"
    ack_topic, ack_body = ack
    packet = mqtt_packet(0x30, mqtt_string(ack_topic) + ack_body)
    diagnostic_log("application_ack_publish_attempt", data, outcome)
    try:
        sock.sendall(packet)
    except OSError as exc:
        diagnostic_log("application_ack_publish_socket_write_failed", data, outcome,
                       type(exc).__name__)
        raise
    diagnostic_log("application_ack_publish_socket_write_complete", data, outcome)
    return True


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
                        newly_inserted = ingest(topic, payload)
                        if topic.endswith("/event"):
                            publish_committed_event_ack(sock, topic, payload, newly_inserted)
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
        if parsed.path == "/api/sync-status":
            return self.send_json(sync_status())
        if parsed.path == "/api/notifications":
            return self.send_json({"status": notification_status(), "log": notification_log()})
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
        path = urlparse(self.path).path
        if path == "/api/sync":
            if not SYNC_API_TOKEN:
                self.send_json({"error": "sync receiver is not configured"}, 503); return
            authorization = self.headers.get("Authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            if scheme.lower() != "bearer" or not supplied or not hmac.compare_digest(supplied, SYNC_API_TOKEN):
                self.send_json({"error": "unauthorized"}, 401); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "invalid content length"}, 400); return
            if not 0 < length <= 512 * 1024:
                self.send_json({"error": "invalid request size"}, 413); return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                accepted = receive_sync_batch(body)
            except RuntimeError:
                self.send_json({"error": "sync identity conflict"}, 409); return
            except (ValueError, UnicodeDecodeError, KeyError, TypeError):
                self.send_json({"error": "invalid sync batch"}, 400); return
            self.send_json({"accepted_source_ids": accepted}); return
        if path != "/api/incidents/ack":
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

    def send_json(self, value: object, status: int = 200) -> None:
        content = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)


if __name__ == "__main__":
    validate_web_bind()
    init_db()
    threading.Thread(target=mqtt_worker, daemon=True).start()
    threading.Thread(target=retention_worker, daemon=True).start()
    if SYNC_URL and SYNC_TOKEN:
        threading.Thread(target=sync_worker, daemon=True).start()
    if NOTIFICATION_PROVIDER == "mock" and NOTIFICATION_RECIPIENTS:
        threading.Thread(target=notification_worker, daemon=True).start()
    print(f"Dashboard: http://{WEB_HOST}:{WEB_PORT}", flush=True)
    ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler).serve_forever()
