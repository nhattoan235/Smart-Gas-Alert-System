import json
import io
import tempfile
import time
import unittest
from pathlib import Path

import server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB_PATH = Path(self.tmp.name) / "test.sqlite3"
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

    def test_rejects_spoofed_or_bad_payload(self):
        wrong = {**self.message, "device_id": "node3"}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "lpg": {**self.message["lpg"], "signal_pct": 999}}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "state": "DANGER"}
        self.assertFalse(server.ingest(self.topic, json.dumps(wrong).encode()))
        wrong = {**self.message, "schema": 1}
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
        self.assertEqual(metric["algorithm"]["tp"], 1)
        self.assertEqual(metric["fixed"]["fp"], 1)
        self.assertEqual(metric["episodes"]["episodes"], 1)
        self.assertEqual(metric["episodes"]["detected_episodes"], 1)

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


if __name__ == "__main__":
    unittest.main()
