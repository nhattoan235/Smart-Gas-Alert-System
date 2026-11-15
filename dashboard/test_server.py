import json
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / "test.sqlite3"
        self.original_config = {name: getattr(server, name) for name in (
            "SYNC_URL", "SYNC_TOKEN", "SYNC_API_TOKEN", "SYNC_ALLOW_HTTP", "SYNC_BATCH_SIZE",
            "SYNC_RETRY_BASE_S", "NOTIFICATION_PROVIDER", "NOTIFICATION_RECIPIENTS",
            "NOTIFY_RETRY_BASE_S", "NOTIFY_MAX_ATTEMPTS", "TELEMETRY_RETENTION_DAYS",
            "EVENT_RETENTION_DAYS", "SYNC_RECEIPT_RETENTION_DAYS", "NOTIFICATION_RETENTION_DAYS",
            "ESCALATE_AFTER_S")}
        server.init_db()
        self.topic = f"home/{server.HOME_ID}/device/node1/telemetry"
        self.message = {"schema": 2, "kind": "telemetry", "home_id": server.HOME_ID,
                        "device_id": "node1", "boot_id": "abc", "seq": 1,
                        "state": "SAFE", "changed_gas": "NONE",
                        "lpg": {"state": "SAFE", "signal_pct": 3.5, "baseline_pct": 3,
                                "rise_pct": .5, "valid": True},
                        "co": {"state": "SAFE", "signal_pct": 2.2, "baseline_pct": 2,
                               "rise_pct": .2, "valid": True},
                        "temperature_c": 27, "humidity_pct": 60, "simulated": True}

    def tearDown(self):
        for name, value in self.original_config.items():
            setattr(server, name, value)
        self.tmp.cleanup()

    def test_ingest_snapshot_dedup_and_event(self):
        body = json.dumps(self.message).encode()
        self.assertTrue(server.ingest(self.topic, body))
        self.assertFalse(server.ingest(self.topic, body))
        snap = server.snapshot()
        node = next(n for n in snap["nodes"] if n["device_id"] == "node1")
        self.assertTrue(node["online"])
        self.assertEqual(node["state"], "SAFE")
        self.assertEqual(node["latest"]["lpg"]["signal_pct"], 3.5)
        self.assertEqual(node["latest"]["co"]["signal_pct"], 2.2)
        self.assertEqual(len(server.history("node1")), 1)
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "CO",
                 "co": {**self.message["co"], "state": "WARNING"}}
        self.assertTrue(server.ingest(self.topic.replace("telemetry", "event"), json.dumps(event).encode()))
        self.assertEqual(len(server.events()), 1)

    def test_device_commit_ack_is_separate_from_server_sync_ack(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        payload = json.dumps(event).encode()
        self.assertTrue(server.ingest(topic, payload))
        self.assertEqual(server.committed_event_ack(topic, payload),
                         (f"home/{server.HOME_ID}/device/node1/ack", b"abc:2"))
        with server.db() as con:
            self.assertIsNone(con.execute("SELECT server_acked_at FROM messages").fetchone()[0])

    def test_ingest_diagnostic_logs_follow_commit_and_report_dedupe_outcome(self):
        event = {**self.message, "kind": "event", "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        output = io.StringIO()
        commit_visibility = []
        write_log = server.diagnostic_log

        def observe_commit(event, data, dedupe_outcome, error_type=None):
            with server.db() as con:
                stored = con.execute("SELECT payload FROM messages WHERE device_id=? AND boot_id=? AND seq=?",
                                     (data["device_id"], data["boot_id"], data["seq"])).fetchone()
            commit_visibility.append(stored is not None)
            write_log(event, data, dedupe_outcome, error_type)

        with patch.object(server, "diagnostic_log", side_effect=observe_commit):
            with patch("sys.stdout", output):
                self.assertTrue(server.ingest(topic, json.dumps(event).encode()))
                self.assertFalse(server.ingest(topic, json.dumps(event).encode()))
        lines = output.getvalue().splitlines()
        self.assertEqual(commit_visibility, [True, True])
        records = [json.loads(line) for line in lines]
        self.assertEqual([record["event"] for record in records],
                         ["sqlite_commit_complete", "sqlite_commit_complete"])
        self.assertEqual([record["dedupe_outcome"] for record in records], ["new", "duplicate"])
        for record in records:
            self.assertEqual((record["device_id"], record["boot_id"], record["seq"], record["kind"]),
                             ("node1", "abc", 1, "event"))
            self.assertTrue(record["timestamp_utc"].endswith("+00:00"))
            self.assertIn("queue_count", record)
            self.assertIsNone(record["queue_count"])
            self.assertNotIn("token", json.dumps(record).lower())

        telemetry_output = io.StringIO()
        telemetry = {**self.message, "seq": 3}
        with patch("sys.stdout", telemetry_output):
            self.assertTrue(server.ingest(self.topic, json.dumps(telemetry).encode()))
            self.assertFalse(server.ingest(self.topic, json.dumps(telemetry).encode()))
        self.assertEqual(telemetry_output.getvalue(), "")

    def test_application_ack_requires_committed_matching_payload_and_logs_socket_write(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        payload = json.dumps(event).encode()
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertTrue(server.ingest(topic, payload))

        class RecordingSocket:
            def __init__(self): self.packets = []
            def sendall(self, packet): self.packets.append(packet)

        sock = RecordingSocket()
        with patch("sys.stdout", output):
            self.assertTrue(server.publish_committed_event_ack(sock, topic, payload, True))
            altered = {**event, "lpg": {**event["lpg"], "signal_pct": 9.0}}
            altered_payload = json.dumps(altered).encode()
            self.assertFalse(server.ingest(topic, altered_payload))
            self.assertFalse(server.publish_committed_event_ack(sock, topic, altered_payload, False))
            self.assertFalse(server.ingest(topic, payload))
            self.assertTrue(server.publish_committed_event_ack(sock, topic, payload, False))
        self.assertEqual(len(sock.packets), 2)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["event"] for record in records], [
            "sqlite_commit_complete", "application_ack_publish_attempt",
            "application_ack_publish_socket_write_complete",
            "sqlite_commit_complete", "application_ack_publish_not_sent",
            "sqlite_commit_complete", "application_ack_publish_attempt",
            "application_ack_publish_socket_write_complete",
        ])
        self.assertEqual([record["dedupe_outcome"] for record in records],
                         ["new", "new", "new", "identity_conflict", "identity_conflict",
                          "duplicate", "duplicate", "duplicate"])
        self.assertEqual(records[4]["reason"], "stored_payload_mismatch")
        for record in records:
            self.assertEqual((record["device_id"], record["boot_id"], record["seq"]),
                             ("node1", "abc", 2))
            self.assertTrue(record["timestamp_utc"].endswith("+00:00"))
            self.assertIn("queue_count", record)
            self.assertIsNone(record["queue_count"])
        self.assertNotIn("signal_pct", output.getvalue())
        expected_packet = server.mqtt_packet(
            0x30, server.mqtt_string(f"home/{server.HOME_ID}/device/node1/ack") + b"abc:2")
        self.assertEqual(sock.packets, [expected_packet, expected_packet])

    def test_application_ack_socket_failure_logs_attempt_without_success(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        payload = json.dumps(event).encode()
        with patch("sys.stdout", io.StringIO()):
            self.assertTrue(server.ingest(topic, payload))

        class FailingSocket:
            def sendall(self, _packet): raise OSError("private transport detail")

        output = io.StringIO()
        with patch("sys.stdout", output):
            with self.assertRaises(OSError):
                server.publish_committed_event_ack(FailingSocket(), topic, payload, True)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["event"] for record in records], [
            "application_ack_publish_attempt", "application_ack_publish_socket_write_failed",
        ])
        self.assertEqual([record["dedupe_outcome"] for record in records], ["new", "new"])
        self.assertEqual(records[1]["error_type"], "OSError")
        self.assertTrue(all("queue_count" in record and record["queue_count"] is None for record in records))
        self.assertNotIn("private transport detail", output.getvalue())

    def test_commit_ack_rejects_same_identity_with_different_event_payload(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        self.assertTrue(server.ingest(topic, json.dumps(event).encode()))
        altered = {**event, "lpg": {**event["lpg"], "signal_pct": 9.0}}
        self.assertIsNone(server.committed_event_ack(topic, json.dumps(altered).encode()))

    def test_rejects_spoofed_or_bad_payload(self):
        wrong = {**self.message, "device_id": "node3"}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "lpg": {**self.message["lpg"], "signal_pct": 999}}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "state": "DANGER"}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "schema": 1}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        for key in ("valve_open_sim", "stove_on_sim"):
            wrong = {**self.message, key: "false"}
            self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        self.assertFalse(server.ingest(self.topic, b"not json"))
        self.assertEqual(len(server.history("node1")), 0)

    def test_independent_gas_fault_keeps_node_in_fault(self):
        message = {**self.message, "state": "FAULT",
                   "co": {**self.message["co"], "state": "FAULT", "valid": False}}
        self.assertTrue(server.ingest(self.topic, json.dumps(message).encode()))
        node = next(n for n in server.snapshot()["nodes"] if n["device_id"] == "node1")
        self.assertEqual(node["state"], "FAULT")
        self.assertEqual(node["latest"]["lpg"]["state"], "SAFE")

    def test_incident_escalation_ack_and_close(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        self.assertTrue(server.ingest(topic, json.dumps(event).encode()))
        self.assertEqual(server.committed_event_ack(topic, json.dumps(event).encode()),
                         (f"home/{server.HOME_ID}/device/node1/ack", b"abc:2"))
        # QoS 1 may redeliver an event when the application ACK was lost.
        # Keep the SQLite row idempotent, but send the commit ACK again.
        self.assertFalse(server.ingest(topic, json.dumps(event).encode()))
        self.assertEqual(server.committed_event_ack(topic, json.dumps(event).encode()),
                         (f"home/{server.HOME_ID}/device/node1/ack", b"abc:2"))
        self.assertEqual(len(server.events()), 1)
        self.assertIsNone(server.committed_event_ack(topic, b"invalid"))
        incident_id = server.incidents()[0]["id"]
        with server.db() as con:
            con.execute("UPDATE incidents SET opened_at=opened_at-120 WHERE id=?", (incident_id,))
        self.assertIsNotNone(server.incidents()[0]["escalated_at"])
        self.assertTrue(server.acknowledge_incident(incident_id))
        self.assertFalse(server.acknowledge_incident(incident_id))
        clear = {**event, "seq": 3, "state": "SAFE", "lpg": self.message["lpg"]}
        self.assertTrue(server.ingest(topic, json.dumps(clear).encode()))
        self.assertIsNotNone(server.incidents()[0]["closed_at"])

    def test_warmup_is_not_a_gas_incident(self):
        warmup = {**self.message, "kind": "event", "state": "WARMUP", "changed_gas": "BOTH",
                  "lpg": {**self.message["lpg"], "state": "WARMUP"},
                  "co": {**self.message["co"], "state": "WARMUP"}}
        self.assertTrue(server.ingest(self.topic.replace("telemetry", "event"), json.dumps(warmup).encode()))
        self.assertEqual(server.incidents(), [])

    def test_valve_reminder_requires_continuous_condition(self):
        now = time.time()
        with server.db() as con:
            for i in range(66):
                data = {**self.message, "seq": i + 1, "valve_open_sim": True, "stove_on_sim": False}
                at = now - 130 + i * 2
                con.execute("INSERT INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload) VALUES(?,?,?,?,?,?,?,?)",
                            ("sample", at, "node1", "abc", i + 1, "telemetry", "SAFE", json.dumps(data)))
        node = next(n for n in server.snapshot()["nodes"] if n["device_id"] == "node1")
        self.assertTrue(node["valve_reminder"])
        with server.db() as con:
            data = {**self.message, "seq": 67, "valve_open_sim": True, "stove_on_sim": True}
            con.execute("INSERT INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload) VALUES(?,?,?,?,?,?,?,?)",
                        ("sample", now, "node1", "abc", 67, "telemetry", "SAFE", json.dumps(data)))
        node = next(n for n in server.snapshot()["nodes"] if n["device_id"] == "node1")
        self.assertFalse(node["valve_reminder"])

    def test_labeled_simulation_metrics(self):
        result = server.binary_metrics([True, True, False, False], [True, False, True, False])
        self.assertEqual({key: result[key] for key in ("tp", "fp", "fn", "tn")},
                         {"tp": 1, "fp": 1, "fn": 1, "tn": 1})
        self.assertEqual(result["precision"], .5)
        self.assertEqual(result["recall"], .5)
        self.assertIsNone(server.binary_metrics([False], [False])["precision"])
        positive = {**self.message, "lpg_leak_truth_sim": True, "co_hazard_truth_sim": False,
                    "state": "WATCH", "lpg": {**self.message["lpg"], "state": "WATCH", "signal_pct": 10}}
        negative = {**self.message, "seq": 2, "lpg_leak_truth_sim": False,
                    "co_hazard_truth_sim": False, "lpg": {**self.message["lpg"], "signal_pct": 15}}
        self.assertTrue(server.ingest(self.topic, json.dumps(positive).encode()))
        self.assertTrue(server.ingest(self.topic, json.dumps(negative).encode()))
        metric = next(row for row in server.evaluation() if row["device_id"] == "node1" and row["gas"] == "LPG")
        self.assertEqual(metric["samples"], 2)
        self.assertEqual(metric["excluded_invalid_samples"], 0)
        self.assertEqual(metric["algorithm"]["tp"], 1)
        self.assertEqual(metric["fixed"]["fp"], 1)
        self.assertEqual(metric["episodes"]["episodes"], 1)
        self.assertEqual(metric["episodes"]["detected_episodes"], 1)

    def test_evaluation_excludes_invalid_channels_independently(self):
        now = time.time()
        samples = [
            # Invalid LPG truth starts an episode but must not start its delay clock.
            (10, now - 40, "SAFE", False, 100, True, "SAFE", True, 1, False),
            (11, now - 30, "WATCH", True, 10, True, "SAFE", True, 1, False),
            # Invalid CO warning must not create an FP; valid LPG remains eligible.
            (12, now - 20, "SAFE", True, 1, False, "WARNING", False, 100, False),
            (13, now - 10, "SAFE", True, 1, False, "WARNING", True, 35, True),
        ]
        with server.db() as con:
            for seq, at, lpg_state, lpg_valid, lpg_signal, lpg_truth, co_state, co_valid, co_signal, co_truth in samples:
                state = max((lpg_state, co_state), key=server.SEVERITY.get)
                data = {**self.message, "seq": seq, "state": state,
                        "lpg_leak_truth_sim": lpg_truth, "co_hazard_truth_sim": co_truth,
                        "lpg": {**self.message["lpg"], "state": lpg_state, "valid": lpg_valid,
                                "signal_pct": lpg_signal},
                        "co": {**self.message["co"], "state": co_state, "valid": co_valid,
                               "signal_pct": co_signal}}
                con.execute("INSERT INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload) VALUES(?,?,?,?,?,?,?,?)",
                            (server.datetime.fromtimestamp(at, server.timezone.utc).isoformat(), at,
                             "node1", "eval", seq, "telemetry", state, json.dumps(data)))
        metrics = {row["gas"]: row for row in server.evaluation() if row["device_id"] == "node1"}
        lpg = metrics["LPG"]
        co = metrics["CO"]
        self.assertEqual((lpg["samples"], lpg["excluded_invalid_samples"]), (3, 1))
        self.assertEqual((lpg["algorithm"]["tp"], lpg["algorithm"]["fp"], lpg["algorithm"]["fn"]), (1, 0, 0))
        self.assertEqual(lpg["episodes"], {"episodes": 1, "detected_episodes": 1, "mean_delay_s": 0})
        self.assertEqual((co["samples"], co["excluded_invalid_samples"]), (3, 1))
        self.assertEqual((co["algorithm"]["tp"], co["algorithm"]["fp"], co["algorithm"]["fn"]), (1, 0, 0))
        self.assertEqual(co["episodes"], {"episodes": 1, "detected_episodes": 1, "mean_delay_s": 0})

    def test_episode_delay_ignores_unrecognized_episode(self):
        metric = server.episode_delays([(0, False, False), (2, True, False),
                                        (4, True, True), (6, False, False),
                                        (8, True, False), (10, False, False)])
        self.assertEqual(metric, {"episodes": 2, "detected_episodes": 1, "mean_delay_s": 2})

    def test_mqtt_packet_framing(self):
        self.assertEqual(server.mqtt_packet(0xC0, b""), b"\xc0\x00")
        packet = server.mqtt_packet(0x30, b"x" * 130)
        self.assertEqual(packet[:3], b"\x30\x82\x01")

    def test_snapshot_reports_broker_connection(self):
        original = server.MQTT_CONNECTED
        try:
            server.MQTT_CONNECTED = False
            self.assertFalse(server.snapshot()["broker"]["connected"])
            server.MQTT_CONNECTED = True
            self.assertTrue(server.snapshot()["broker"]["connected"])
        finally:
            server.MQTT_CONNECTED = original

    def test_snapshot_api_route_returns_three_offline_nodes(self):
        handler = object.__new__(server.Handler)
        handler.path = "/api/snapshot"
        responses = []
        handler.send_json = responses.append
        handler.do_GET()
        self.assertEqual(len(responses), 1)
        self.assertEqual(len(responses[0]["nodes"]), 3)
        self.assertTrue(all(not node["online"] for node in responses[0]["nodes"]))

    def test_bootstrap_asset_is_served_locally(self):
        handler = object.__new__(server.Handler)
        handler.path = "/vendor/bootstrap.min.css"
        handler.wfile = io.BytesIO()
        responses = []
        handler.send_response = lambda code: responses.append(code)
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.do_GET()
        self.assertEqual(responses, [200])
        self.assertIn(b"Bootstrap", handler.wfile.getvalue()[:300])

    def test_web_bind_fails_closed_outside_loopback(self):
        server.validate_web_bind("127.0.0.1")
        server.validate_web_bind("::1")
        server.validate_web_bind("localhost")
        with self.assertRaisesRegex(RuntimeError, "TLS/auth reverse proxy"):
            server.validate_web_bind("0.0.0.0")
        server.validate_web_bind("0.0.0.0", allow_insecure=True)

    def test_sync_sender_advances_only_explicit_server_ack_ids(self):
        second = {**self.message, "seq": 2, "lpg": {**self.message["lpg"], "signal_pct": 4.5}}
        self.assertTrue(server.ingest(self.topic, json.dumps(self.message).encode()))
        self.assertTrue(server.ingest(self.topic, json.dumps(second).encode()))
        server.SYNC_URL = "https://sync.example.test/api/sync"
        server.SYNC_TOKEN = "test-secret"
        sent = {}

        class FakeResponse:
            def __init__(self, body):
                self.body = body
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _limit=-1): return self.body

        def fake_urlopen(request, timeout):
            sent["authorization"] = request.unredirected_hdrs.get("Authorization")
            sent["timeout"] = timeout
            sent["batch"] = json.loads(request.data)
            acknowledged = sent["batch"]["messages"][0]["source_id"]
            return FakeResponse(json.dumps({"accepted_source_ids": [acknowledged]}).encode())

        original_urlopen = server.urllib.request.urlopen
        try:
            server.urllib.request.urlopen = fake_urlopen
            self.assertEqual(server.sync_once(), 1)
        finally:
            server.urllib.request.urlopen = original_urlopen
        self.assertEqual(sent["authorization"], "Bearer test-secret")
        self.assertEqual(len(sent["batch"]["messages"]), 2)
        self.assertEqual(sent["timeout"], 8)
        acknowledged = sent["batch"]["messages"][0]["source_id"]
        with server.db() as con:
            rows = con.execute("SELECT payload,server_acked_at,sync_attempts FROM messages ORDER BY seq").fetchall()
        marked = {server.stable_source_id(json.loads(row["payload"])): row for row in rows}
        self.assertIsNotNone(marked[acknowledged]["server_acked_at"])
        remaining = [row for key, row in marked.items() if key != acknowledged]
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(remaining[0]["server_acked_at"])
        self.assertEqual(remaining[0]["sync_attempts"], 1)
        status = server.sync_status()
        self.assertEqual(status["pending"], 1)
        self.assertNotIn("token", json.dumps(status).lower())

    def test_loopback_http_sync_commits_receiver_and_acks_gateway(self):
        self.assertTrue(server.ingest(self.topic, json.dumps(self.message).encode()))
        receiver_tmp = tempfile.TemporaryDirectory()
        receiver_db = Path(receiver_tmp.name) / "receiver.sqlite3"
        ready_file = Path(receiver_tmp.name) / "receiver-ready.txt"
        script = """
import os
import sys
import threading
import server
from http.server import ThreadingHTTPServer

server.init_db()
class QuietHandler(server.Handler):
    def log_message(self, *_args):
        pass

httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
serve_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
serve_thread.start()
with open(os.environ["GAS_TEST_READY_FILE"], "w", encoding="ascii") as ready:
    ready.write(str(httpd.server_port))
    ready.flush()
sys.stdin.read()
httpd.shutdown()
httpd.server_close()
serve_thread.join(timeout=3)
"""
        receiver_env = os.environ.copy()
        receiver_env["GAS_DB"] = str(receiver_db)
        receiver_env["GAS_SYNC_API_TOKEN"] = "loopback-test-token"
        receiver_env["GAS_TEST_READY_FILE"] = str(ready_file)
        receiver_env["WEB_HOST"] = "127.0.0.1"
        receiver_proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=str(Path(server.__file__).resolve().parent),
            env=receiver_env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
        )
        server.SYNC_API_TOKEN = "loopback-test-token"
        server.SYNC_TOKEN = "loopback-test-token"
        server.SYNC_ALLOW_HTTP = True
        deadline = time.monotonic() + 8
        try:
            while not ready_file.exists():
                if receiver_proc.poll() is not None:
                    stderr = receiver_proc.stderr.read() if receiver_proc.stderr else ""
                    self.fail(f"Receiver subprocess failed to start: {stderr[-1000:]}")
                if time.monotonic() >= deadline:
                    self.fail("Receiver subprocess did not become ready")
                time.sleep(.02)
            receiver_port = int(ready_file.read_text(encoding="ascii"))
            server.SYNC_URL = f"http://127.0.0.1:{receiver_port}/api/sync"
            source_id = server.stable_source_id(self.message)
            # A count of one means the receiver explicitly returned this source ID.
            self.assertEqual(server.sync_once(), 1)
            with server.db() as con:
                gateway_row = con.execute("SELECT server_acked_at FROM messages WHERE seq=?", (self.message["seq"],)).fetchone()
            self.assertIsNotNone(gateway_row["server_acked_at"])

            receiver_con = server.sqlite3.connect(receiver_db)
            receiver_con.row_factory = server.sqlite3.Row
            try:
                received = receiver_con.execute("SELECT source_id,payload_hash FROM sync_received").fetchone()
                receiver_row = receiver_con.execute("SELECT payload,server_acked_at FROM messages WHERE seq=?", (self.message["seq"],)).fetchone()
            finally:
                receiver_con.close()
            self.assertEqual(received["source_id"], source_id)
            self.assertEqual(received["payload_hash"], server.payload_digest(self.message))
            self.assertIn("signal_pct", receiver_row["payload"])
            self.assertIsNotNone(receiver_row["server_acked_at"])
        finally:
            if receiver_proc.poll() is None:
                try:
                    if receiver_proc.stdin:
                        receiver_proc.stdin.close()
                    receiver_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    receiver_proc.terminate()
                    receiver_proc.wait(timeout=3)
            if receiver_proc.stderr:
                receiver_proc.stderr.close()
            receiver_tmp.cleanup()
        self.assertEqual(receiver_proc.returncode, 0)

    def test_sync_retries_offline_batch_with_same_source_id_then_acknowledges(self):
        self.assertTrue(server.ingest(self.topic, json.dumps(self.message).encode()))
        server.SYNC_URL = "https://sync.example.test/api/sync"
        server.SYNC_TOKEN = "test-secret"
        server.SYNC_RETRY_BASE_S = 1
        now = time.time()
        clock = {"now": now}
        request_ids = []

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _limit=-1):
                return json.dumps({"accepted_source_ids": [request_ids[-1]]}).encode()

        def flaky_urlopen(request, timeout):
            body = json.loads(request.data)
            request_ids.append(body["messages"][0]["source_id"])
            if len(request_ids) == 1:
                raise server.urllib.error.URLError("secret response must not be persisted")
            return FakeResponse()

        original_urlopen = server.urllib.request.urlopen
        try:
            server.urllib.request.urlopen = flaky_urlopen
            with patch.object(server.time, "time", side_effect=lambda: clock["now"]):
                self.assertEqual(server.sync_once(), 0)
                with server.db() as con:
                    failed = con.execute("SELECT server_acked_at,sync_attempts,sync_next_attempt_at,sync_last_error FROM messages").fetchone()
                self.assertIsNone(failed["server_acked_at"])
                self.assertEqual(failed["sync_attempts"], 1)
                self.assertGreater(failed["sync_next_attempt_at"], now)
                self.assertEqual(failed["sync_last_error"], "URLError")
                self.assertEqual(server.sync_once(), 0)
                clock["now"] = now + 2
                self.assertEqual(server.sync_once(), 1)
        finally:
            server.urllib.request.urlopen = original_urlopen
        self.assertEqual(request_ids, [server.stable_source_id(self.message)] * 2)
        with server.db() as con:
            row = con.execute("SELECT server_acked_at,sync_attempts,sync_last_error FROM messages").fetchone()
        self.assertIsNotNone(row["server_acked_at"])
        self.assertEqual(row["sync_attempts"], 2)
        self.assertIsNone(row["sync_last_error"])

    def test_sync_receiver_is_idempotent_and_rejects_source_id_conflicts(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        batch = {"home_id": server.HOME_ID, "messages": [{
            "source_id": server.stable_source_id(event),
            "received_at": "2026-11-12T12:00:00+00:00", "payload": event}]}
        self.assertEqual(server.receive_sync_batch(batch), [batch["messages"][0]["source_id"]])
        self.assertEqual(server.receive_sync_batch(batch), [batch["messages"][0]["source_id"]])
        self.assertEqual(len(server.events()), 1)
        self.assertEqual(len(server.incidents()), 1)
        conflict = json.loads(json.dumps(batch))
        conflict["messages"][0]["payload"]["lpg"]["signal_pct"] = 7.5
        later = {**event, "seq": 3, "lpg": {**event["lpg"], "signal_pct": 8.0}}
        partial_then_conflict = {"home_id": server.HOME_ID, "messages": [
            {"source_id": server.stable_source_id(later), "received_at": "2026-11-12T12:01:00+00:00", "payload": later},
            conflict["messages"][0]]}
        with self.assertRaises(RuntimeError):
            server.receive_sync_batch(partial_then_conflict)
        with server.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sync_received").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertIsNotNone(con.execute("SELECT server_acked_at FROM messages").fetchone()[0])
        server.SYNC_URL = "https://sync.example.test/api/sync"
        server.SYNC_TOKEN = "test-secret"
        original_urlopen = server.urllib.request.urlopen
        try:
            server.urllib.request.urlopen = lambda *_args, **_kwargs: self.fail("receiver re-forwarded an accepted row")
            self.assertEqual(server.sync_once(), 0)
        finally:
            server.urllib.request.urlopen = original_urlopen

    def test_sync_received_retains_hash_only_after_message_retention(self):
        server.TELEMETRY_RETENTION_DAYS = 1
        now = time.time()
        old_time = now - 3 * 86400
        old_data = {**self.message, "seq": 88}
        high_priority = {**self.message, "seq": 89, "state": "WARNING", "changed_gas": "LPG",
                         "lpg": {**self.message["lpg"], "state": "WARNING"}}
        received_at = server.datetime.fromtimestamp(old_time, server.timezone.utc).isoformat()
        source_id = server.stable_source_id(old_data)
        high_source_id = server.stable_source_id(high_priority)
        batch = {"home_id": server.HOME_ID, "messages": [
            {"source_id": source_id, "received_at": received_at, "payload": old_data},
            {"source_id": high_source_id, "received_at": received_at, "payload": high_priority}]}
        self.assertEqual(server.receive_sync_batch(batch), [source_id, high_source_id])
        self.assertEqual(server.prune_retention(now)["telemetry_deleted"], 1)
        with server.db() as con:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(sync_received)")}
        self.assertNotIn("payload", columns)
        with server.db() as con:
            receipt = con.execute("SELECT payload_hash FROM sync_received WHERE source_id=?", (source_id,)).fetchone()
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(receipt["payload_hash"], server.payload_digest(old_data))
        self.assertEqual(server.receive_sync_batch(batch), [source_id, high_source_id])
        with server.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(server.prune_retention(now + 366 * 86400)["sync_receipts_deleted"], 1)
        with server.db() as con:
            remaining_ids = {row["source_id"] for row in con.execute("SELECT source_id FROM sync_received")}
            remaining_messages = {row["seq"] for row in con.execute("SELECT seq FROM messages")}
        self.assertEqual(remaining_ids, {high_source_id})
        self.assertEqual(remaining_messages, {89})

    def test_sync_received_migrates_legacy_full_payload_to_hash(self):
        server.SYNC_RECEIPT_RETENTION_DAYS = 1
        canonical = server.canonical_payload(self.message)
        with server.db() as con:
            con.execute("DROP TABLE sync_received")
            con.execute("""CREATE TABLE sync_received (
                source_id TEXT PRIMARY KEY, received_at REAL NOT NULL, home_id TEXT NOT NULL,
                device_id TEXT NOT NULL, boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL)""")
            con.execute("INSERT INTO sync_received VALUES(?,?,?,?,?,?,?,?)",
                        (server.stable_source_id(self.message), time.time(), server.HOME_ID,
                         self.message["device_id"], self.message["boot_id"], self.message["seq"],
                         self.message["kind"], canonical))
        server.init_db()
        with server.db() as con:
            row = con.execute("SELECT payload,payload_hash,message_pruned_at FROM sync_received").fetchone()
        self.assertEqual(row["payload"], "")
        self.assertEqual(row["payload_hash"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertIsNotNone(row["message_pruned_at"])
        self.assertEqual(server.prune_retention(row["message_pruned_at"] + .99 * 86400)["sync_receipts_deleted"], 0)
        self.assertEqual(server.prune_retention(row["message_pruned_at"] + 1.01 * 86400)["sync_receipts_deleted"], 1)

    def test_receipt_dedupe_horizon_starts_when_source_message_is_pruned(self):
        server.TELEMETRY_RETENTION_DAYS = 1
        server.SYNC_RECEIPT_RETENTION_DAYS = 1
        now = time.time()
        old = now - 2 * 86400
        data = {**self.message, "seq": 91}
        source_id = server.stable_source_id(data)
        received_at = server.datetime.fromtimestamp(old, server.timezone.utc).isoformat()
        with server.db() as con:
            con.execute("INSERT INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload,server_acked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (received_at, old, data["device_id"], data["boot_id"], data["seq"], data["kind"], data["state"], json.dumps(data), old))
            con.execute("INSERT INTO sync_received(source_id,received_at,home_id,device_id,boot_id,seq,kind,payload_hash) VALUES(?,?,?,?,?,?,?,?)",
                        (source_id, old, server.HOME_ID, data["device_id"], data["boot_id"], data["seq"], data["kind"], server.payload_digest(data)))
        result = server.prune_retention(now)
        self.assertEqual(result["telemetry_deleted"], 1)
        self.assertEqual(result["sync_receipts_deleted"], 0)
        with server.db() as con:
            receipt = con.execute("SELECT message_pruned_at FROM sync_received WHERE source_id=?", (source_id,)).fetchone()
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertGreaterEqual(receipt["message_pruned_at"], now)
        batch = {"home_id": server.HOME_ID, "messages": [{
            "source_id": source_id, "received_at": received_at, "payload": data}]}
        self.assertEqual(server.receive_sync_batch(batch), [source_id])
        with server.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(server.prune_retention(now + .99 * 86400)["sync_receipts_deleted"], 0)
        self.assertEqual(server.prune_retention(now + 1.01 * 86400)["sync_receipts_deleted"], 1)

    def test_sync_http_receiver_requires_bearer_token(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        request_body = json.dumps({"home_id": server.HOME_ID, "messages": [{
            "source_id": server.stable_source_id(event), "received_at": "2026-11-12T12:00:00+00:00", "payload": event}]}).encode()

        class Headers(dict):
            def get(self, name, default=None):
                return super().get(name, default)

        def handler_for(authorization):
            handler = object.__new__(server.Handler)
            handler.path = "/api/sync"
            handler.headers = Headers({"Authorization": authorization, "Content-Length": str(len(request_body))})
            handler.rfile = io.BytesIO(request_body)
            handler.responses = []
            handler.send_json = lambda body, status=200: handler.responses.append((status, body))
            return handler

        server.SYNC_API_TOKEN = "receiver-secret"
        denied = handler_for("Bearer wrong")
        denied.do_POST()
        self.assertEqual(denied.responses, [(401, {"error": "unauthorized"})])
        allowed = handler_for("Bearer receiver-secret")
        allowed.do_POST()
        self.assertEqual(allowed.responses[0][0], 200)
        self.assertEqual(allowed.responses[0][1]["accepted_source_ids"], [server.stable_source_id(event)])

    def test_notification_outbox_waits_for_ack_expiry_and_logs_simulation(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        self.assertTrue(server.ingest(self.topic.replace("telemetry", "event"), json.dumps(event).encode()))
        incident_id = server.incidents()[0]["id"]
        server.NOTIFICATION_PROVIDER = "mock"
        server.NOTIFICATION_RECIPIENTS = ("owner@example.test",)
        self.assertEqual(server.queue_expired_notifications(), 0)
        with server.db() as con:
            con.execute("UPDATE incidents SET opened_at=? WHERE id=?", (time.time() - 120, incident_id))
        self.assertEqual(server.queue_expired_notifications(), 1)
        self.assertEqual(server.queue_expired_notifications(), 0)
        self.assertEqual(server.process_notification_outbox(), 1)
        status = server.notification_status()
        self.assertEqual(status["simulated_deliveries"], 1)
        self.assertTrue(status["simulated"])
        log = server.notification_log()
        self.assertEqual(log[0]["status"], "simulated")
        self.assertIn("no external message sent", log[0]["detail"])
        self.assertNotIn("owner@example.test", json.dumps(log))
        self.assertNotIn("owner@example.test", json.dumps(status))
        with server.db() as con:
            row = con.execute("SELECT status,attempts FROM notification_outbox").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("simulated", 1))

    def test_notification_retry_reuses_deduplicated_outbox_row(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        self.assertTrue(server.ingest(self.topic.replace("telemetry", "event"), json.dumps(event).encode()))
        server.NOTIFICATION_PROVIDER = "mock"
        server.NOTIFICATION_RECIPIENTS = ("owner@example.test",)
        server.NOTIFY_RETRY_BASE_S = 1
        with server.db() as con:
            con.execute("UPDATE incidents SET opened_at=?", (time.time() - 120,))

        class FlakyProvider:
            name = "mock"
            def __init__(self): self.calls = 0
            def send(self, *_):
                self.calls += 1
                if self.calls == 1: raise TimeoutError("provider secret must not be logged")
                return {"simulated": True}

        provider = FlakyProvider()
        now = time.time()
        self.assertEqual(server.process_notification_outbox(provider=provider, now=now), 1)
        self.assertEqual(server.process_notification_outbox(provider=provider, now=now + 2), 1)
        self.assertEqual(server.process_notification_outbox(provider=provider, now=now + 3), 0)
        with server.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            row = con.execute("SELECT status,attempts,last_error FROM notification_outbox").fetchone()
            self.assertEqual(con.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0], 2)
        self.assertEqual((row["status"], row["attempts"], row["last_error"]), ("simulated", 2, None))
        self.assertNotIn("provider secret", json.dumps(server.notification_log()))

    def test_pending_notification_is_cancelled_when_incident_is_acknowledged(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        self.assertTrue(server.ingest(self.topic.replace("telemetry", "event"), json.dumps(event).encode()))
        server.NOTIFICATION_PROVIDER = "mock"
        server.NOTIFICATION_RECIPIENTS = ("owner@example.test",)
        with server.db() as con:
            incident_id = con.execute("SELECT id FROM incidents").fetchone()["id"]
            con.execute("UPDATE incidents SET opened_at=? WHERE id=?", (time.time() - 120, incident_id))
        self.assertEqual(server.queue_expired_notifications(), 1)
        self.assertTrue(server.acknowledge_incident(incident_id))
        self.assertEqual(server.process_notification_outbox(), 0)
        with server.db() as con:
            row = con.execute("SELECT status,last_error FROM notification_outbox").fetchone()
        self.assertEqual((row["status"], row["last_error"]), ("cancelled", "incident acknowledged"))

    def test_pending_notification_is_cancelled_when_incident_closes(self):
        event = {**self.message, "kind": "event", "seq": 2, "state": "WARNING", "changed_gas": "LPG",
                 "lpg": {**self.message["lpg"], "state": "WARNING"}}
        topic = self.topic.replace("telemetry", "event")
        self.assertTrue(server.ingest(topic, json.dumps(event).encode()))
        server.NOTIFICATION_PROVIDER = "mock"
        server.NOTIFICATION_RECIPIENTS = ("owner@example.test",)
        with server.db() as con:
            incident_id = con.execute("SELECT id FROM incidents").fetchone()["id"]
            con.execute("UPDATE incidents SET opened_at=? WHERE id=?", (time.time() - 120, incident_id))
        self.assertEqual(server.queue_expired_notifications(), 1)
        clear = {**event, "seq": 3, "state": "SAFE", "lpg": self.message["lpg"]}
        self.assertTrue(server.ingest(topic, json.dumps(clear).encode()))
        self.assertEqual(server.process_notification_outbox(), 0)
        with server.db() as con:
            row = con.execute("SELECT status,last_error FROM notification_outbox").fetchone()
        self.assertEqual((row["status"], row["last_error"]), ("cancelled", "incident closed"))

    def test_metadata_retention_prunes_only_closed_terminal_notifications(self):
        server.NOTIFICATION_RETENTION_DAYS = 1
        now = time.time()
        old = now - 3 * 86400
        with server.db() as con:
            closed_id = con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state,closed_at) VALUES('node1','lpg',?,'closed',1,'WARNING',?)",
                                    (old, old + 10)).lastrowid
            open_id = con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state) VALUES('node2','co',?,'open',2,'WARNING')",
                                  (old,)).lastrowid
            sending_closed_id = con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state,closed_at) VALUES('node3','co',?,'sending',3,'WARNING',?)",
                                            (old, old + 10)).lastrowid
            recently_closed_id = con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state,closed_at) VALUES('node3','lpg',?,'recent',4,'WARNING',?)",
                                             (old, now - .5 * 86400)).lastrowid
            outbox = {}
            for incident_id, key, status, attempted_at in (
                (closed_id, "closed-terminal", "simulated", now),
                (open_id, "open-terminal", "simulated", old),
                (open_id, "open-pending", "pending", old),
                (sending_closed_id, "closed-sending", "sending", old),
                (recently_closed_id, "recent-terminal", "simulated", old),
            ):
                outbox[key] = con.execute("""INSERT INTO notification_outbox
                    (incident_id,recipient,dedupe_key,payload,status,attempts,created_at,next_attempt_at,delivered_at)
                    VALUES(?,?,?,?,?,1,?,?,?)""",
                    (incident_id, "owner@example.test", key, "{}", status, old, old,
                     old if status == "simulated" else None)).lastrowid
                con.execute("INSERT INTO notification_log(outbox_id,attempted_at,provider,recipient,status,detail) VALUES(?,?,?,?,?,?)",
                            (outbox[key], attempted_at, "mock", "owner@example.test", "failed" if status in ("pending", "sending") else "simulated", "old attempt"))
        result = server.prune_retention(now)
        self.assertEqual(result["notification_outbox_deleted"], 1)
        self.assertEqual(result["notification_logs_deleted"], 1)
        with server.db() as con:
            states = {row["dedupe_key"]: row["status"] for row in con.execute("SELECT dedupe_key,status FROM notification_outbox")}
            self.assertEqual(con.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0], 4)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 4)
        self.assertEqual(states, {"open-terminal": "simulated", "open-pending": "pending",
                                  "closed-sending": "sending", "recent-terminal": "simulated"})

    def test_retention_prunes_only_old_acknowledged_low_priority_records(self):
        server.TELEMETRY_RETENTION_DAYS = 1
        server.EVENT_RETENTION_DAYS = 1
        now = time.time()
        old = now - 3 * 86400
        with server.db() as con:
            rows = [
                ("node1", "safe-old", 1, "telemetry", "SAFE", 1),
                ("node1", "safe-pending", 2, "telemetry", "SAFE", None),
                ("node1", "warning-old", 3, "telemetry", "WARNING", 1),
                ("node1", "safe-event", 4, "event", "SAFE", 1),
                ("node1", "alarm-event", 5, "event", "WARNING", 1),
            ]
            for device, boot, seq, kind, state, acked in rows:
                data = {**self.message, "device_id": device, "boot_id": boot, "seq": seq, "kind": kind, "state": state}
                con.execute("INSERT INTO messages(received_at,received_epoch,device_id,boot_id,seq,kind,state,payload,server_acked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            ("old", old, device, boot, seq, kind, state, json.dumps(data), old if acked else None))
            con.execute("INSERT INTO incidents(device_id,gas,opened_at,opened_boot_id,opened_seq,state) VALUES('node1','lpg',?,?,?,'WARNING')",
                        (old, "alarm-event", 5))
        result = server.prune_retention(now)
        self.assertEqual(result, {"telemetry_deleted": 1, "events_deleted": 1,
                                  "sync_receipts_deleted": 0, "notification_logs_deleted": 0,
                                  "notification_outbox_deleted": 0})
        with server.db() as con:
            remaining = {(row["boot_id"], row["seq"]) for row in con.execute("SELECT boot_id,seq FROM messages")}
        self.assertEqual(remaining, {("safe-pending", 2), ("warning-old", 3), ("alarm-event", 5)})


if __name__ == "__main__":
    unittest.main()
