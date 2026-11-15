from __future__ import annotations

import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import firmware_nvs_replay as driver


class NvsRuntimeDriverProtocolTests(unittest.TestCase):
    def _workspace_tempdir(self):
        temp_root = driver.REPO / "tmp" / "nvs-runtime-driver-unit"
        temp_root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=temp_root)

    def test_event_diag_parser_preserves_identity_and_queue_count(self) -> None:
        line = (
            "event_diag device_id=node1 boot_id=abc123 seq=17 identity=app_ack "
            "millis=1200 stage=app_ack outcome=matched_remove_ok queue_count=2 "
            "packet_id=0 detail=pointers_saved"
        )
        parsed = driver.parse_event_diag(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed["device_id"], parsed["boot_id"], parsed["seq"]),
                         ("node1", "abc123", 17))
        self.assertEqual((parsed["outcome"], parsed["queue_count"]),
                         ("matched_remove_ok", 2))

    def test_status_parser_requires_output_and_queue_observations(self) -> None:
        line = (
            "nvs_test_status device_id=node1 boot_id=abc123 seq=20 queue_count=3 "
            "mqtt_connected=1 lpg_override=4095 red_pin=1 buzzer_duty=511"
        )
        parsed = driver.parse_status(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["queue_count"], 3)
        self.assertEqual(parsed["red_pin"], 1)
        self.assertEqual(parsed["buzzer_duty"], 511)
        self.assertIsNone(driver.parse_status("nvs_test_status device_id=node1 queue_count=0"))

    def test_status_parser_includes_test_sensor_invalid_flags_when_reported(self) -> None:
        line = (
            "nvs_test_status device_id=node1 boot_id=abc seq=20 queue_count=3 "
            "mqtt_connected=0 lpg_override=-1 lpg_invalid_test=1 co_invalid_test=0 "
            "red_pin=1 buzzer_duty=511"
        )
        parsed = driver.parse_status(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["lpg_invalid_test"], 1)
        self.assertEqual(parsed["co_invalid_test"], 0)

    def test_reboot_validation_accepts_reset_ram_override_when_nvs_queue_is_preserved(self) -> None:
        status = {
            "boot_id": "boot-new",
            "queue_count": 1,
            "lpg_override": -1,
        }
        override = driver.validate_pending_queue_after_reboot(
            status, previous_boot_id="boot-old", expected_queue_count=1
        )
        self.assertEqual(override, -1)

    def test_reboot_validation_rejects_changed_boot_or_lost_nvs_queue(self) -> None:
        same_boot = {"boot_id": "boot-old", "queue_count": 1, "lpg_override": -1}
        with self.assertRaisesRegex(AssertionError, "boot_id did not change"):
            driver.validate_pending_queue_after_reboot(same_boot, "boot-old", 1)

        lost_queue = {"boot_id": "boot-new", "queue_count": 0, "lpg_override": -1}
        with self.assertRaisesRegex(AssertionError, "queue count changed across reboot"):
            driver.validate_pending_queue_after_reboot(lost_queue, "boot-old", 1)

    def test_rfc2217_partial_reads_are_reassembled_before_status_parsing(self) -> None:
        pending = bytearray()
        first = b"nvs_test_status device_i"
        second = (
            b"d=node1 boot_id=abc123 seq=20 queue_count=3 mqtt_connected=1 "
            b"lpg_override=4095 red_pin=1 buzzer_duty=511\r\n"
        )
        self.assertEqual(driver.extract_complete_serial_lines(pending, first), [])
        self.assertEqual(pending, first)
        lines = driver.extract_complete_serial_lines(pending, second)
        self.assertEqual(len(lines), 1)
        self.assertEqual(pending, b"")
        parsed = driver.parse_status(lines[0].decode("utf-8").strip())
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed["device_id"], parsed["boot_id"], parsed["seq"]),
                         ("node1", "abc123", 20))

    def test_serial_framer_handles_multiple_lines_and_keeps_trailing_fragment(self) -> None:
        pending = bytearray()
        lines = driver.extract_complete_serial_lines(pending, b"first\r\nsecond\npart")
        self.assertEqual(lines, [b"first\r\n", b"second\n"])
        self.assertEqual(pending, b"part")
        self.assertEqual(driver.extract_complete_serial_lines(pending, b"ial\n"), [b"partial\n"])

    def test_serial_framer_rejects_unterminated_line_over_limit(self) -> None:
        pending = bytearray()
        with self.assertRaisesRegex(driver.SerialLineTooLong, "exceeded 8 bytes"):
            driver.extract_complete_serial_lines(pending, b"123456789", max_line_bytes=8)

    def test_serial_monitor_does_not_publish_partial_status_line(self) -> None:
        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class FragmentingPort:
            def __init__(self) -> None:
                self.chunks = [
                    b"nvs_test_status device_i",
                    b"d=node1 boot_id=abc123 seq=20 queue_count=3 mqtt_connected=1 "
                    b"lpg_override=4095 red_pin=1 buzzer_duty=511\n",
                ]
                self.closed = threading.Event()

            def readline(self):
                if self.chunks:
                    return self.chunks.pop(0)
                self.closed.wait(0.01)
                return b""

            def write(self, _payload):
                return 1

            def flush(self):
                return None

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port = FragmentingPort()
            log = FakeLog()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", log, Path(temp_dir) / "serial.log"
                )
                try:
                    _index, line = monitor.wait_line(
                        lambda item: "nvs_test_status" in item,
                        timeout=1,
                        description="fragmented NVS status",
                    )
                    self.assertIsNotNone(driver.parse_status(line))
                    self.assertEqual(len(monitor.lines), 1)
                    logged = [fields["detail"] for stage, fields in log.records
                              if stage == "firmware_serial"]
                    self.assertEqual(len(logged), 1)
                    self.assertIn("device_id=node1", logged[0])
                finally:
                    monitor.close()

    def test_enqueue_payload_identity_must_match_log_fields(self) -> None:
        payload = {"kind": "event", "device_id": "node1", "boot_id": "abc123", "seq": 17}
        encoded = json.dumps(payload, separators=(",", ":"))
        line = f"nvs_test_event_record device_id=node1 boot_id=abc123 seq=17 payload={encoded}"
        parsed = driver.parse_event_record(line)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(json.loads(parsed[1]), payload)
        mismatch = f"nvs_test_event_record device_id=node1 boot_id=abc123 seq=18 payload={encoded}"
        with self.assertRaises(AssertionError):
            driver.parse_event_record(mismatch)

    def test_interleaved_wifi_diagnostic_does_not_hide_chunked_fault_payload(self) -> None:
        payload = {
            "kind": "event", "device_id": "node1", "boot_id": "boot-fault", "seq": 9,
            "state": "FAULT",
            "lpg": {"state": "FAULT", "valid": False},
            "co": {"state": "SAFE", "valid": True},
            "valve_open_sim": False,
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        interleaved_wifi_log = (
            '[ 18593][E][WiFiClient.cpp:275] connect(): socket error, "Connection reset by peer"'
        )
        corrupted_legacy_line = (
            "nvs_test_event_record device_id=node1 boot_id=boot-fault seq=9 payload="
            + encoded[:80]
            + interleaved_wifi_log
        )
        self.assertIsNone(driver.parse_event_record(corrupted_legacy_line))
        self.assertFalse(driver.sensor_fault_event_matches(corrupted_legacy_line, "LPG", "SAFE"))
        same_line_corruption = (
            "nvs_test_event_record device_id=node1 boot_id=boot-fault seq=9 payload="
            + encoded[:80] + interleaved_wifi_log + encoded[80:]
        )
        self.assertIsNone(driver.parse_event_record(same_line_corruption))

        chunk_size = 72
        chunks = [encoded[offset:offset + chunk_size]
                  for offset in range(0, len(encoded), chunk_size)]
        identity = "device_id=node1 boot_id=boot-fault seq=9"
        lines = [f"nvs_test_event_record_begin {identity} payload_bytes={len(encoded)} chunks={len(chunks)}"]
        for index, chunk in enumerate(chunks):
            lines.append(f"nvs_test_event_record_chunk {identity} part={index} data={chunk}")
            if index == 1:
                lines.append(interleaved_wifi_log)
        lines.append(f"nvs_test_event_record_end {identity} chunks={len(chunks)}")

        assembler = driver.SerialEventRecordAssembler()
        reconstructed = [record for line in lines
                         if (record := assembler.feed(line)) is not None]
        self.assertEqual(len(reconstructed), 1)
        parsed = driver.parse_event_record(reconstructed[0])
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[1], encoded.encode("utf-8"))
        self.assertTrue(driver.sensor_fault_event_matches(reconstructed[0], "LPG", "SAFE"))

    def test_serial_monitor_reassembles_interleaved_event_before_wait_predicate(self) -> None:
        payload = {
            "kind": "event", "device_id": "node1", "boot_id": "boot-fault", "seq": 9,
            "state": "FAULT", "lpg": {"state": "FAULT", "valid": False},
            "co": {"state": "SAFE", "valid": True},
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        chunks = [encoded[offset:offset + 48] for offset in range(0, len(encoded), 48)]
        identity = "device_id=node1 boot_id=boot-fault seq=9"
        lines = [f"nvs_test_event_record_begin {identity} payload_bytes={len(encoded)} chunks={len(chunks)}"]
        for index, value in enumerate(chunks):
            lines.append(f"nvs_test_event_record_chunk {identity} part={index} data={value}")
            if index == 0:
                lines.append('[ 18593][E][WiFiClient.cpp:275] connect(): socket error, "reset"')
        lines.append(f"nvs_test_event_record_end {identity} chunks={len(chunks)}")

        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class FakePort:
            def __init__(self) -> None:
                self.lines = [line.encode("utf-8") + b"\n" for line in lines]
                self.closed = threading.Event()

            def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                self.closed.wait(0.01)
                return b""

            def write(self, _payload):
                return 1

            def flush(self):
                return None

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port, log = FakePort(), FakeLog()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", log, Path(temp_dir) / "serial.log"
                )
                try:
                    _index, record = monitor.wait_line(
                        lambda item: driver.sensor_fault_event_matches(item, "LPG", "SAFE"),
                        timeout=1,
                        description="reassembled LPG FAULT event",
                    )
                    parsed = driver.parse_event_record(record)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertEqual(parsed[1], encoded.encode("utf-8"))
                    self.assertTrue(any(stage == "serial_event_record_reassembled"
                                        for stage, _fields in log.records))
                finally:
                    monitor.close()

    def test_serial_monitor_fails_fast_on_incomplete_event_record(self) -> None:
        identity = "device_id=node1 boot_id=boot-fault seq=9"
        lines = [
            f"nvs_test_event_record_begin {identity} payload_bytes=20 chunks=2",
            f"nvs_test_event_record_chunk {identity} part=0 data={{\"kind\":\"event\"",
            f"nvs_test_event_record_end {identity} chunks=2",
        ]

        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class FakePort:
            def __init__(self) -> None:
                self.lines = [line.encode("utf-8") + b"\n" for line in lines]
                self.closed = threading.Event()

            def readline(self):
                if self.lines:
                    return self.lines.pop(0)
                self.closed.wait(0.01)
                return b""

            def write(self, _payload):
                return 1

            def flush(self):
                return None

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port, log = FakePort(), FakeLog()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", log, Path(temp_dir) / "serial.log"
                )
                started = time.monotonic()
                try:
                    with self.assertRaisesRegex(AssertionError, "missing chunks"):
                        monitor.wait_line(lambda _line: False, timeout=10,
                                          description="should fail on malformed event framing")
                    self.assertLess(time.monotonic() - started, 1.0)
                    self.assertTrue(any(stage == "serial_event_record_reassembly_error"
                                        for stage, _fields in log.records))
                finally:
                    monitor.close()

    def test_event_assembler_rejects_wifi_text_inserted_inside_chunk(self) -> None:
        payload = {
            "kind": "event", "device_id": "node1", "boot_id": "boot-fault", "seq": 9,
            "state": "FAULT",
        }
        encoded = json.dumps(payload, separators=(",", ":"))
        identity = "device_id=node1 boot_id=boot-fault seq=9"
        lines = [
            f"nvs_test_event_record_begin {identity} payload_bytes={len(encoded)} chunks=1",
            f"nvs_test_event_record_chunk {identity} part=0 data={encoded[:12]}"
            '[ 18698][E][WiFiClient.cpp:275] connect(): socket error'
            f"{encoded[12:]}",
            f"nvs_test_event_record_end {identity} chunks=1",
        ]
        assembler = driver.SerialEventRecordAssembler()
        assembler.feed(lines[0])
        with self.assertRaisesRegex(ValueError, "byte length mismatch"):
            assembler.feed(lines[1])
            assembler.feed(lines[2])

    def test_nvs_runtime_firmware_event_writer_uses_short_indexed_chunks(self) -> None:
        source = (driver.REPO / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        helper_start = source.index("static void printNvsRuntimeTestEventRecord")
        helper_end = source.index("static void processNvsRuntimeTestCommand", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("constexpr size_t chunkBytes=16", helper)
        self.assertIn("nvs_test_event_record_begin", helper)
        self.assertIn("nvs_test_event_record_chunk", helper)
        self.assertIn("nvs_test_event_record_end", helper)

    def test_only_nvs_runtime_target_suppresses_core_log_interleaving(self) -> None:
        config = (driver.REPO / "firmware" / "platformio.ini").read_text(encoding="utf-8")
        start = config.index("[env:node1_nvs_runtime]")
        end = config.find("\n[env:", start + 1)
        section = config[start:end if end >= 0 else len(config)]
        self.assertIn("-D CORE_DEBUG_LEVEL=0", section)
        self.assertIn("[env:node1]", config)
        normal_start = config.index("[env:node1]")
        normal_end = config.find("\n[env:", normal_start + 1)
        normal_section = config[normal_start:normal_end if normal_end >= 0 else len(config)]
        self.assertNotIn("CORE_DEBUG_LEVEL=0", normal_section)

        source = (driver.REPO / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        self.assertIn(
            '#if !NVS_RUNTIME_TEST\n          Serial.println("MQTT connect failed: check gateway, address and credentials");\n#endif',
            source,
        )

    def test_nvs_queue_snapshot_preserves_fifo_identity_and_payload(self) -> None:
        payload = {"kind": "event", "device_id": "node1", "boot_id": "boot-old", "seq": 7,
                   "state": "SAFE"}
        encoded = json.dumps(payload, separators=(",", ":"))
        lines = [
            "nvs_test_queue_snapshot_begin device_id=node1 boot_id=boot-current queue_count=1 head=3 capacity=16",
            f"nvs_test_queue_record device_id=node1 event_boot_id=boot-old event_seq=7 index=0 slot=3 queue_count=1 payload={encoded}",
            "nvs_test_queue_snapshot_end device_id=node1 boot_id=boot-current queue_count=1 records=1 outcome=complete",
        ]
        result = driver.parse_nvs_queue_snapshot(lines)
        self.assertEqual(result["queue_count"], 1)
        self.assertEqual((result["records"][0]["device_id"], result["records"][0]["boot_id"],
                          result["records"][0]["seq"], result["records"][0]["slot"]),
                         ("node1", "boot-old", 7, 3))
        self.assertEqual(json.loads(result["records"][0]["payload"]), payload)

    def test_lost_ack_validation_allows_unrelated_enqueue_while_original_event_remains(self) -> None:
        pending = {"kind": "event", "device_id": "node1", "boot_id": "old-boot", "seq": 7,
                   "state": "DANGER"}
        unrelated = {"kind": "event", "device_id": "node1", "boot_id": "new-boot", "seq": 1,
                     "state": "SAFE"}
        pending_bytes = json.dumps(pending, separators=(",", ":")).encode("utf-8")
        snapshot = {
            "queue_count": 2,  # a new SAFE event was enqueued after the dropped ACK
            "records": [
                {"device_id": "node1", "boot_id": "old-boot", "seq": 7, "payload": pending_bytes},
                {"device_id": "node1", "boot_id": "new-boot", "seq": 1,
                 "payload": json.dumps(unrelated, separators=(",", ":")).encode("utf-8")},
            ],
        }
        self.assertEqual(
            driver.validate_event_retained_after_lost_ack(
                snapshot, ("node1", "old-boot", 7), pending_bytes
            ),
            2,
        )

    def test_lost_ack_validation_rejects_missing_or_changed_pending_event(self) -> None:
        expected = b'{"kind":"event","device_id":"node1","boot_id":"old-boot","seq":7}'
        identity = ("node1", "old-boot", 7)
        with self.assertRaisesRegex(AssertionError, "missing after dropped application ACK"):
            driver.validate_event_retained_after_lost_ack({"queue_count": 1, "records": []}, identity, expected)
        changed = {"device_id": "node1", "boot_id": "old-boot", "seq": 7,
                   "payload": b'{"kind":"event","device_id":"node1","boot_id":"old-boot","seq":7,"state":"SAFE"}'}
        with self.assertRaisesRegex(AssertionError, "payload changed"):
            driver.validate_event_retained_after_lost_ack({"queue_count": 1, "records": [changed]}, identity, expected)

    def test_lost_ack_runtime_uses_nvs_identity_snapshot_not_exact_count(self) -> None:
        source = (driver.REPO / "tools" / "e2e" / "firmware_nvs_replay.py").read_text(encoding="utf-8")
        self.assertIn("after_drop = monitor.queue_snapshot()", source)
        self.assertIn("validate_event_retained_after_lost_ack(", source)
        self.assertNotIn(
            'if after_drop["queue_count"] != queue_count_before_reconnect:',
            source,
        )

    def test_payload_normalizer_accepts_text_and_already_encoded_bytes(self) -> None:
        self.assertEqual(driver.payload_to_bytes(b"event"), b"event")
        self.assertEqual(driver.payload_to_bytes(bytearray(b"event")), b"event")
        self.assertEqual(driver.payload_to_bytes("event"), b"event")

    def test_payload_normalizer_rejects_unknown_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "str, bytes, or bytearray"):
            driver.payload_to_bytes({"payload": "event"})

    def test_nvs_queue_snapshot_rejects_missing_fifo_record(self) -> None:
        lines = [
            "nvs_test_queue_snapshot_begin device_id=node1 boot_id=boot-current queue_count=1 head=3 capacity=16",
            "nvs_test_queue_snapshot_end device_id=node1 boot_id=boot-current queue_count=1 records=0 outcome=complete",
        ]
        with self.assertRaisesRegex(AssertionError, "record count mismatch"):
            driver.parse_nvs_queue_snapshot(lines)

    def test_nvs_queue_snapshot_checks_boot_capacity_and_wrapped_fifo(self) -> None:
        lines = [
            "nvs_test_queue_snapshot_begin device_id=node1 boot_id=boot-current queue_count=3 head=6 capacity=8",
        ]
        for index, slot, seq in ((0, 6, 21), (1, 7, 22), (2, 0, 23)):
            payload = {"kind": "event", "device_id": "node1", "boot_id": "old-boot", "seq": seq}
            encoded = json.dumps(payload, separators=(",", ":"))
            lines.append(
                f"nvs_test_queue_record device_id=node1 event_boot_id=old-boot event_seq={seq} "
                f"index={index} slot={slot} queue_count=3 payload={encoded}"
            )
        lines.append(
            "nvs_test_queue_snapshot_end device_id=node1 boot_id=boot-current queue_count=3 records=3 outcome=complete"
        )
        result = driver.parse_nvs_queue_snapshot(lines)
        self.assertEqual([row["slot"] for row in result["records"]], [6, 7, 0])
        self.assertEqual([row["seq"] for row in result["records"]], [21, 22, 23])

        bad_boot = list(lines)
        bad_boot[-1] = bad_boot[-1].replace("boot-current", "different-boot")
        with self.assertRaisesRegex(AssertionError, "boot identities disagree"):
            driver.parse_nvs_queue_snapshot(bad_boot)
        bad_capacity = [line.replace("capacity=8", "capacity=0") for line in lines]
        with self.assertRaisesRegex(AssertionError, "ring capacity"):
            driver.parse_nvs_queue_snapshot(bad_capacity)

    def test_nvs_queue_snapshot_rejects_duplicate_identity(self) -> None:
        payload = {"kind": "event", "device_id": "node1", "boot_id": "old-boot", "seq": 21}
        encoded = json.dumps(payload, separators=(",", ":"))
        lines = [
            "nvs_test_queue_snapshot_begin device_id=node1 boot_id=current queue_count=2 head=6 capacity=8",
            f"nvs_test_queue_record device_id=node1 event_boot_id=old-boot event_seq=21 index=0 slot=6 queue_count=2 payload={encoded}",
            f"nvs_test_queue_record device_id=node1 event_boot_id=old-boot event_seq=21 index=1 slot=7 queue_count=2 payload={encoded}",
            "nvs_test_queue_snapshot_end device_id=node1 boot_id=current queue_count=2 records=2 outcome=complete",
        ]
        with self.assertRaisesRegex(AssertionError, "duplicate event identity"):
            driver.parse_nvs_queue_snapshot(lines)

    def test_firmware_snapshot_command_and_dump_are_test_target_only(self) -> None:
        source = (driver.REPO / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        handler_start = source.index("#if NVS_RUNTIME_TEST\nstatic constexpr uint32_t NVS_TEST_RTC_MAGIC")
        handler_end = source.index("#endif", handler_start)
        handler = source[handler_start:handler_end]
        self.assertIn('strcmp(command,"NVS_TEST DUMP_QUEUE")', handler)
        self.assertIn("dumpNvsRuntimeTestQueue();", handler)
        function_start = source.index("#if NVS_RUNTIME_TEST\nstatic void dumpNvsRuntimeTestQueue()")
        function_end = source.index("#endif", function_start)
        function = source[function_start:function_end]
        self.assertIn("eventStore.getString", function)
        self.assertIn("nvs_test_queue_snapshot_begin", function)
        self.assertIn("nvs_test_queue_snapshot_end", function)

    def test_sensor_invalid_serial_control_reaches_test_only_input_validity(self) -> None:
        source = (driver.REPO / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        test_controls_start = source.index("#if NVS_RUNTIME_TEST\nstatic constexpr uint32_t NVS_TEST_RTC_MAGIC")
        test_controls_end = source.index("#endif", test_controls_start)
        test_controls = source[test_controls_start:test_controls_end]
        self.assertIn('NVS_TEST SET_SENSOR_INVALID %11s %d %c', test_controls)
        self.assertIn('strcmp(sensor,"LPG")', test_controls)
        self.assertIn('strcmp(sensor,"CO")', test_controls)
        self.assertIn('strcmp(sensor,"BOTH")', test_controls)

        read_inputs_start = source.index("static void readInputs()")
        read_inputs_end = source.index("#if HARDWARE_N8R8 && N8R8_SENSOR_CONFIGURED\nstatic void pollHardwareSensors", read_inputs_start)
        read_inputs = source[read_inputs_start:read_inputs_end]
        simulation = read_inputs.split("#elif HARDWARE_N8R8", 1)[0]
        self.assertIn("#if NVS_RUNTIME_TEST", simulation)
        self.assertIn("if(nvsTestLpgInvalid) lpgValid=false;", simulation)
        self.assertIn("if(nvsTestCoInvalid) coValid=false;", simulation)

    def test_fault_event_match_requires_invalid_gas_and_fault_state(self) -> None:
        payload = {
            "kind": "event", "device_id": "node1", "boot_id": "boot", "seq": 9,
            "state": "FAULT",
            "lpg": {"state": "FAULT", "valid": False},
            "co": {"state": "SAFE", "valid": True},
        }
        line = (
            "nvs_test_event_record device_id=node1 boot_id=boot seq=9 payload="
            + json.dumps(payload, separators=(",", ":"))
        )
        self.assertTrue(driver.sensor_fault_event_matches(line, "LPG", "SAFE"))
        self.assertFalse(driver.sensor_fault_event_matches(line, "CO", "WATCH"))
        valid_payload = {**payload, "seq": 10, "lpg": {"state": "FAULT", "valid": True}}
        valid_line = (
            "nvs_test_event_record device_id=node1 boot_id=boot seq=10 payload="
            + json.dumps(valid_payload, separators=(",", ":"))
        )
        self.assertFalse(driver.sensor_fault_event_matches(valid_line, "LPG", "SAFE"))

    def test_sensor_invalid_control_sends_targeted_serial_command(self) -> None:
        class FakeMonitor:
            def __init__(self) -> None:
                self.command = None
                self.response = None
                self.flags = {"lpg_invalid_test": 0, "co_invalid_test": 0}

            def send(self, command, response):
                self.command = command
                self.response = response(
                    "nvs_test_control outcome=accepted command=set_sensor_invalid sensor=CO invalid=1"
                )
                self.flags["co_invalid_test"] = 1
                return 0, "accepted"

            def status(self):
                return self.flags

        monitor = FakeMonitor()
        status = driver.set_sensor_invalid(monitor, "CO", True)
        self.assertEqual(monitor.command, "NVS_TEST SET_SENSOR_INVALID CO 1")
        self.assertTrue(monitor.response)
        self.assertEqual(status["co_invalid_test"], 1)

    def test_phase_parser_keeps_danger_default_and_combined_order(self) -> None:
        parser = driver.build_argument_parser()
        self.assertEqual(parser.parse_args([]).phase, "danger")
        self.assertEqual(driver.runtime_phase_sequence("danger"), ("danger",))
        self.assertEqual(driver.runtime_phase_sequence("fault-offline"), ("fault-offline",))
        combined = parser.parse_args(["--phase", "both"])
        self.assertEqual(combined.phase, "both")
        self.assertEqual(driver.runtime_phase_sequence(combined.phase), ("danger", "fault-offline"))

    def _startup_backlog_fixture(self):
        identity = ("node1", "boot-old", 7)
        payload = {"kind": "event", "device_id": identity[0], "boot_id": identity[1],
                   "seq": identity[2], "state": "SAFE"}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        snapshot = {"device_id": "node1", "boot_id": "boot-current", "queue_count": 1,
                    "records": [{"device_id": identity[0], "boot_id": identity[1], "seq": identity[2],
                                 "index": 0, "slot": 3, "queue_count": 1, "payload": raw}]}
        published = [{"device_id": identity[0], "boot_id": identity[1], "seq": identity[2],
                      "payload": raw}]
        acks = [{"device_id": identity[0], "boot_id": identity[1], "seq": identity[2],
                 "dropped": False, "sqlite_rows_at_publish": 1,
                 "timestamp_utc": "2026-11-15T10:00:01+00:00"}]
        sqlite_rows = [{"device_id": identity[0], "boot_id": identity[1],
                        "seq": identity[2], "payload": raw.decode("utf-8")}]
        backend = [
            {"event": "sqlite_commit_complete", "device_id": identity[0], "boot_id": identity[1],
             "seq": identity[2], "timestamp_utc": "2026-11-15T10:00:00+00:00"},
            {"event": "application_ack_publish_attempt", "device_id": identity[0], "boot_id": identity[1],
             "seq": identity[2], "timestamp_utc": "2026-11-15T10:00:01+00:00"},
        ]
        serial = [
            "event_diag device_id=node1 boot_id=boot-old seq=7 identity=app_ack millis=200 stage=app_ack outcome=received_by_main queue_count=1 packet_id=0 detail=commit_ack_queue",
            "event_diag device_id=node1 boot_id=boot-old seq=7 identity=app_ack millis=210 stage=app_ack outcome=matched_remove_ok queue_count=0 packet_id=0 detail=pointers_saved",
        ]
        return snapshot, published, acks, sqlite_rows, backend, serial, identity

    def test_startup_backlog_requires_full_publish_commit_ack_and_remove_chain(self) -> None:
        snapshot, published, acks, rows, backend, serial, identity = self._startup_backlog_fixture()
        removed = driver.validate_startup_backlog_replay(
            snapshot, published, acks, rows, backend, serial, starting_cursor=0
        )
        self.assertEqual(removed, [identity])

    def test_startup_backlog_rejects_missing_mqtt_publish(self) -> None:
        snapshot, _published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        with self.assertRaisesRegex(AssertionError, "never published"):
            driver.validate_startup_backlog_replay(snapshot, [], acks, rows, backend, serial, 0)

    def test_startup_backlog_rejects_ack_before_sqlite_commit(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        backend[1]["timestamp_utc"] = "2026-11-15T09:59:59+00:00"
        with self.assertRaisesRegex(AssertionError, "preceded SQLite commit"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0)

    def test_startup_backlog_rejects_missing_gateway_application_ack(self) -> None:
        snapshot, published, _acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        with self.assertRaisesRegex(AssertionError, "did not publish a committed application ACK"):
            driver.validate_startup_backlog_replay(snapshot, published, [], rows, backend, serial, 0)

    def test_startup_backlog_rejects_duplicate_sqlite_identity(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        with self.assertRaisesRegex(AssertionError, "exactly one committed SQLite row"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows + rows, backend, serial, 0)

    def test_startup_backlog_rejects_dropped_only_application_ack(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        acks[0]["dropped"] = True
        with self.assertRaisesRegex(AssertionError, "did not publish a committed application ACK"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0)

    def test_startup_backlog_rejects_changed_mqtt_payload(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        published[0]["payload"] = b'{"kind":"event","state":"CHANGED"}'
        with self.assertRaisesRegex(AssertionError, "payload changed during replay"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0)

    def test_startup_backlog_requires_all_fifo_removals_and_queue_decrements(self) -> None:
        snapshot, published, acks, rows, backend, serial, identity = self._startup_backlog_fixture()
        identity2 = ("node1", "boot-old", 8)
        payload2 = {"kind": "event", "device_id": identity2[0], "boot_id": identity2[1],
                    "seq": identity2[2], "state": "WARNING"}
        raw2 = json.dumps(payload2, separators=(",", ":")).encode("utf-8")
        snapshot["queue_count"] = 2
        snapshot["records"].append({"device_id": identity2[0], "boot_id": identity2[1], "seq": identity2[2],
                                     "index": 1, "slot": 4, "queue_count": 2, "payload": raw2})
        published.append({"device_id": identity2[0], "boot_id": identity2[1], "seq": identity2[2], "payload": raw2})
        acks.append({"device_id": identity2[0], "boot_id": identity2[1], "seq": identity2[2],
                     "dropped": False, "sqlite_rows_at_publish": 1,
                     "timestamp_utc": "2026-11-15T10:00:03+00:00"})
        rows.append({"device_id": identity2[0], "boot_id": identity2[1], "seq": identity2[2],
                     "payload": raw2.decode("utf-8")})
        backend.extend([
            {"event": "sqlite_commit_complete", "device_id": identity2[0], "boot_id": identity2[1],
             "seq": identity2[2], "timestamp_utc": "2026-11-15T10:00:02+00:00"},
            {"event": "application_ack_publish_attempt", "device_id": identity2[0], "boot_id": identity2[1],
             "seq": identity2[2], "timestamp_utc": "2026-11-15T10:00:03+00:00"},
        ])
        serial = [
            "event_diag device_id=node1 boot_id=boot-old seq=7 identity=app_ack millis=200 stage=app_ack outcome=received_by_main queue_count=2 packet_id=0 detail=commit_ack_queue",
            "event_diag device_id=node1 boot_id=boot-old seq=7 identity=app_ack millis=210 stage=app_ack outcome=matched_remove_ok queue_count=1 packet_id=0 detail=pointers_saved",
            "event_diag device_id=node1 boot_id=boot-old seq=8 identity=app_ack millis=220 stage=app_ack outcome=received_by_main queue_count=1 packet_id=0 detail=commit_ack_queue",
            "event_diag device_id=node1 boot_id=boot-old seq=8 identity=app_ack millis=230 stage=app_ack outcome=matched_remove_ok queue_count=0 packet_id=0 detail=pointers_saved",
        ]
        self.assertEqual(
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0),
            [identity, identity2],
        )
        reordered_serial = [
            serial[2].replace("queue_count=1", "queue_count=2"),
            serial[3].replace("queue_count=0", "queue_count=1"),
            serial[0].replace("queue_count=2", "queue_count=1"),
            serial[1].replace("queue_count=1", "queue_count=0"),
        ]
        with self.assertRaisesRegex(AssertionError, "FIFO order"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, reordered_serial, 0)

    def test_startup_backlog_rejects_missing_matching_firmware_removal(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        serial = serial[:1]
        with self.assertRaisesRegex(AssertionError, "queue-count|FIFO order"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0)

    def test_startup_backlog_rejects_removal_without_persisted_pointers(self) -> None:
        snapshot, published, acks, rows, backend, serial, _identity = self._startup_backlog_fixture()
        serial[1] = serial[1].replace("detail=pointers_saved", "detail=pointer_write_failed")
        with self.assertRaisesRegex(AssertionError, "persisted queue pointer state"):
            driver.validate_startup_backlog_replay(snapshot, published, acks, rows, backend, serial, 0)

    def test_enqueue_collector_uses_offline_phase_cursor(self) -> None:
        before = {"kind": "event", "device_id": "node1", "boot_id": "boot", "seq": 1}
        after = {"kind": "event", "device_id": "node1", "boot_id": "boot", "seq": 2}
        lines = [
            f"nvs_test_event_record device_id=node1 boot_id=boot seq=1 payload={json.dumps(before)}",
            f"nvs_test_event_record device_id=node1 boot_id=boot seq=2 payload={json.dumps(after)}",
        ]
        records = driver.collect_enqueue_records(lines, start=1)
        self.assertEqual(list(records), [("node1", "boot", 2)])

    def test_first_observed_offline_enqueue_is_fifo_replay_head(self) -> None:
        head = ("node1", "old-boot", 8)
        tail = ("node1", "new-boot", 1)
        records = {head: b'{"seq":8}', tail: b'{"seq":1}'}
        self.assertEqual(driver.first_queued_identity(records), head)

    def test_fifo_head_requires_enqueue_evidence(self) -> None:
        with self.assertRaisesRegex(AssertionError, "without any observed enqueue"):
            driver.first_queued_identity({})

    def test_enqueue_collector_rejects_changed_payload_for_same_identity(self) -> None:
        first = {"kind": "event", "device_id": "node1", "boot_id": "boot", "seq": 2, "state": "DANGER"}
        second = {**first, "state": "WARNING"}
        lines = [
            f"nvs_test_event_record device_id=node1 boot_id=boot seq=2 payload={json.dumps(first)}",
            f"nvs_test_event_record device_id=node1 boot_id=boot seq=2 payload={json.dumps(second)}",
        ]
        with self.assertRaises(AssertionError):
            driver.collect_enqueue_records(lines)

    def test_rfc2217_open_options_omit_unsupported_write_timeout(self) -> None:
        options, scheme, target = driver.serial_open_options("rfc2217://127.0.0.1:4000")
        self.assertEqual(scheme, "rfc2217")
        self.assertEqual(target, "127.0.0.1:4000")
        self.assertEqual(options, {"baudrate": 115200, "timeout": 0.15})
        self.assertNotIn("write_timeout", options)

    def test_rfc2217_debug_url_preserves_endpoint_and_existing_options(self) -> None:
        self.assertEqual(
            driver.serial_url_with_debug("rfc2217://127.0.0.1:4000?timeout=5"),
            "rfc2217://127.0.0.1:4000?timeout=5&logging=debug",
        )
        self.assertEqual(driver.serial_url_with_debug("COM12"), "COM12")

    def test_rfc2217_debug_handler_persists_handshake_and_redacts_secrets(self) -> None:
        with self._workspace_tempdir() as temp_dir:
            runtime_log = driver.RuntimeLog(Path(temp_dir))
            logger = logging.getLogger("pySerial.rfc2217")
            prior_handlers = list(logger.handlers)
            restore = driver.enable_rfc2217_diagnostics(runtime_log)
            try:
                logger.debug("client accepts RFC2217; password=do-not-log")
            finally:
                restore()
            self.assertEqual(logger.handlers, prior_handlers)
            lines = (Path(temp_dir) / "nvs-replay-live.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["stage"], "rfc2217_diagnostic")
            self.assertIn("client accepts RFC2217", entry["detail"])
            self.assertIn("password=<redacted>", entry["detail"])
            self.assertNotIn("do-not-log", lines[0])

    def test_real_pyserial_protocol_rejection_is_logged_and_fails_fast(self) -> None:
        try:
            import serial
        except ImportError as exc:
            self.skipTest(f"pySerial unavailable for loopback protocol regression: {exc}")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        port = listener.getsockname()[1]
        peer_received = threading.Event()

        def hold_non_rfc2217_peer() -> None:
            try:
                conn, _address = listener.accept()
                with conn:
                    conn.settimeout(4)
                    try:
                        conn.recv(256)
                        peer_received.set()
                        time.sleep(3.5)
                    except (OSError, socket.timeout):
                        pass
            except (OSError, socket.timeout):
                pass

        peer = threading.Thread(target=hold_non_rfc2217_peer, daemon=True)
        peer.start()
        with self._workspace_tempdir() as temp_dir:
            runtime_log = driver.RuntimeLog(Path(temp_dir))
            restore = driver.enable_rfc2217_diagnostics(runtime_log)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(driver.SerialTransportBlocked, "RFC2217_PROTOCOL_ERROR"):
                    driver.open_serial_when_ready(
                        serial,
                        driver.serial_url_with_debug(f"rfc2217://127.0.0.1:{port}"),
                        timeout=30,
                        retry_delay=0,
                        log=runtime_log,
                    )
            finally:
                restore()
                listener.close()
                peer.join(timeout=1)
            self.assertTrue(peer_received.is_set(), "the loopback peer did not receive RFC2217 negotiation bytes")
            self.assertLess(time.monotonic() - started, 8, "protocol mismatch must not retry until the 30-second deadline")
            entries = [json.loads(line) for line in (Path(temp_dir) / "nvs-replay-live.log").read_text(encoding="utf-8").splitlines()]
            messages = [entry["detail"] for entry in entries if entry.get("stage") == "rfc2217_diagnostic"]
            self.assertTrue(any(message == "enabled logging" for message in messages), messages)
            self.assertTrue(any(entry.get("stage") == "serial_connect_rejected" for entry in entries), entries)

    def test_native_serial_open_keeps_bounded_pyserial_write_timeout(self) -> None:
        options, scheme, target = driver.serial_open_options("COM12", write_timeout=2.5)
        self.assertEqual(scheme, "native")
        self.assertEqual(target, "COM12")
        self.assertEqual(options, {"baudrate": 115200, "timeout": 0.15, "write_timeout": 2.5})

        class NativeSerial:
            def __init__(self) -> None:
                self.kwargs = None
                self.endpoint = object()

            def serial_for_url(self, _url, **kwargs):
                self.kwargs = kwargs
                return self.endpoint

        native = NativeSerial()
        endpoint = driver.open_serial_when_ready(native, "COM12", timeout=0.1)
        self.assertIs(endpoint, native.endpoint)
        self.assertEqual(native.kwargs["write_timeout"], 2)

    def test_serial_endpoint_retry_waits_for_local_simulator_start(self) -> None:
        class DelayedSerial:
            def __init__(self) -> None:
                self.calls = 0
                self.endpoint = object()
                self.open_options = []

            def serial_for_url(self, *_args, **kwargs):
                self.calls += 1
                self.open_options.append(kwargs)
                if "write_timeout" in kwargs:
                    raise NotImplementedError("write_timeout is currently not supported")
                if self.calls == 1:
                    raise ConnectionRefusedError(10061, "actively refused")
                return self.endpoint

        serial = DelayedSerial()
        endpoint = driver.open_serial_when_ready(
            serial, "rfc2217://127.0.0.1:4000", timeout=1, retry_delay=0
        )
        self.assertIs(endpoint, serial.endpoint)
        self.assertEqual(serial.calls, 2)
        self.assertTrue(all("write_timeout" not in options for options in serial.open_options))

    def test_rfc2217_unsupported_setting_fails_fast_as_transport_blocker(self) -> None:
        class UnsupportedSerial:
            def __init__(self) -> None:
                self.calls = 0

            def serial_for_url(self, *_args, **_kwargs):
                self.calls += 1
                raise NotImplementedError("unsupported RFC2217 option")

        serial = UnsupportedSerial()
        with self.assertRaisesRegex(driver.SerialTransportBlocked,
                                    "SERIAL_DRIVER_CONFIGURATION_ERROR"):
            driver.open_serial_when_ready(
                serial, "rfc2217://127.0.0.1:4000", timeout=30, retry_delay=0
            )
        self.assertEqual(serial.calls, 1)

    def test_rfc2217_protocol_mismatch_is_not_retried(self) -> None:
        class SerialException(Exception):
            pass

        class UnsupportedProtocolSerial:
            def __init__(self) -> None:
                self.calls = 0

            def serial_for_url(self, *_args, **_kwargs):
                self.calls += 1
                raise SerialException("Remote does not seem to support RFC2217 or BINARY mode")

        serial = UnsupportedProtocolSerial()
        with self.assertRaisesRegex(driver.SerialTransportBlocked, "RFC2217_PROTOCOL_ERROR"):
            driver.open_serial_when_ready(
                serial, "rfc2217://127.0.0.1:4000", timeout=30, retry_delay=0
            )
        self.assertEqual(serial.calls, 1)

    def test_rfc2217_protocol_rejection_is_logged_without_retry(self) -> None:
        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class SerialException(Exception):
            pass

        class RejectedSerial:
            def __init__(self) -> None:
                self.calls = 0

            def serial_for_url(self, *_args, **_kwargs):
                self.calls += 1
                raise SerialException("Remote does not seem to support RFC2217 or BINARY mode")

        serial = RejectedSerial()
        log = FakeLog()
        with self.assertRaisesRegex(driver.SerialTransportBlocked, "RFC2217_PROTOCOL_ERROR"):
            driver.open_serial_when_ready(
                serial, "rfc2217://127.0.0.1:4000", timeout=10, retry_delay=0, log=log
            )
        self.assertEqual(serial.calls, 1)
        rejected = [fields for stage, fields in log.records if stage == "serial_connect_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["category"], "RFC2217_PROTOCOL_ERROR")

    def test_missing_rfc2217_endpoint_times_out_as_blocked_with_bounded_retries(self) -> None:
        class MissingEndpoint:
            def __init__(self) -> None:
                self.calls = 0

            def serial_for_url(self, *_args, **_kwargs):
                self.calls += 1
                raise ConnectionRefusedError(111, "connection refused")

        serial = MissingEndpoint()
        started = time.monotonic()
        with self.assertRaisesRegex(driver.SerialTransportBlocked, "RFC2217_ENDPOINT_NOT_OPEN"):
            driver.open_serial_when_ready(
                serial, "rfc2217://127.0.0.1:4000", timeout=0.025, retry_delay=0.003
            )
        elapsed = time.monotonic() - started
        self.assertGreater(serial.calls, 1)
        self.assertLess(elapsed, 0.5)

    def test_rfc2217_connect_timeout_is_reported_separately(self) -> None:
        class TimedOutEndpoint:
            def serial_for_url(self, *_args, **_kwargs):
                raise TimeoutError("connection timed out")

        with self.assertRaisesRegex(driver.SerialTransportBlocked, "SERIAL_CONNECT_TIMEOUT"):
            driver.open_serial_when_ready(
                TimedOutEndpoint(), "rfc2217://127.0.0.1:4000", timeout=0.015, retry_delay=0.002
            )

    def test_invalid_rfc2217_host_or_port_fails_without_retry(self) -> None:
        class NeverCalledSerial:
            def serial_for_url(self, *_args, **_kwargs):
                raise AssertionError("serial backend must not be called for an invalid URL")

        for url in ("rfc2217://:4000", "rfc2217://localhost:notaport"):
            with self.subTest(url=url), self.assertRaisesRegex(
                driver.SerialTransportBlocked, "RFC2217_URL_INVALID"
            ):
                driver.open_serial_when_ready(NeverCalledSerial(), url, timeout=10)

    def test_serial_monitor_close_closes_port_and_joins_reader(self) -> None:
        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class BlockingReadPort:
            def __init__(self) -> None:
                self.closed = threading.Event()
                self.read_started = threading.Event()

            def readline(self):
                self.read_started.set()
                self.closed.wait(1)
                return b""

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port = BlockingReadPort()
            log = FakeLog()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", log, Path(temp_dir) / "serial.log", write_timeout=0.05
                )
                self.assertTrue(port.read_started.wait(0.5))
                reader = monitor._reader
                monitor.close()
                monitor.close()
            self.assertTrue(port.closed.is_set())
            self.assertFalse(reader.is_alive())
            self.assertNotIn("serial_reader_close_timeout", [stage for stage, _ in log.records])

    def test_blocked_write_is_bounded_closes_port_and_stops_threads(self) -> None:
        class FakeLog:
            def __init__(self) -> None:
                self.records = []

            def write(self, stage, **fields) -> None:
                self.records.append((stage, fields))

        class BlockingWritePort:
            def __init__(self) -> None:
                self.closed = threading.Event()
                self.write_started = threading.Event()

            def readline(self):
                self.closed.wait(1)
                return b""

            def write(self, _payload):
                self.write_started.set()
                self.closed.wait(5)
                return 1

            def flush(self):
                return None

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port = BlockingWritePort()
            log = FakeLog()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", log, Path(temp_dir) / "serial.log", write_timeout=0.02
                )
                started = time.monotonic()
                with self.assertRaisesRegex(driver.SerialWriteTimeout, "SERIAL_WRITE_TIMEOUT"):
                    monitor.send("test", lambda _line: False, timeout=0.1)
                elapsed = time.monotonic() - started
                reader = monitor._reader
            self.assertTrue(port.write_started.is_set())
            self.assertTrue(port.closed.is_set())
            self.assertLess(elapsed, 1)
            self.assertFalse(reader.is_alive())
            self.assertFalse(any(thread.name == "nvs-serial-write" and thread.is_alive()
                                 for thread in threading.enumerate()))

    def test_serial_read_error_fails_fast_and_reader_thread_exits(self) -> None:
        class FakeLog:
            def write(self, *_args, **_fields) -> None:
                return None

        class ReadErrorPort:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def readline(self):
                raise OSError("RFC2217 connection reset")

            def close(self):
                self.closed.set()

        with self._workspace_tempdir() as temp_dir:
            port = ReadErrorPort()
            with patch.object(driver, "open_serial_when_ready", return_value=port):
                monitor = driver.SerialMonitor(
                    "rfc2217://127.0.0.1:4000", FakeLog(), Path(temp_dir) / "serial.log"
                )
                started = time.monotonic()
                with self.assertRaisesRegex(driver.SerialTransportBlocked, "SERIAL_READ_ERROR"):
                    monitor.wait_line(lambda _line: False, timeout=5, description="not expected")
                self.assertLess(time.monotonic() - started, 0.5)
                reader = monitor._reader
                monitor.close()
            self.assertTrue(port.closed.is_set())
            self.assertFalse(reader.is_alive())

    def test_runtime_result_log_classifies_prerequisite_as_blocked_and_assertion_as_fail(self) -> None:
        self.assertEqual(driver.runtime_failure_classification(driver.SerialTransportBlocked("no endpoint")),
                         ("BLOCKED", 77))
        self.assertEqual(driver.runtime_failure_classification(AssertionError("wrong identity")), ("FAIL", 1))
        self.assertEqual(driver.runtime_failure_classification(TimeoutError("firmware response missing")),
                         ("FAIL", 1))

    def test_baseline_waits_for_three_consecutive_empty_online_samples(self) -> None:
        class Samples:
            def __init__(self) -> None:
                self.values = iter([
                    {"mqtt_connected": 1, "queue_count": 1},
                    {"mqtt_connected": 1, "queue_count": 0},
                    {"mqtt_connected": 0, "queue_count": 0},
                    {"mqtt_connected": 1, "queue_count": 0},
                    {"mqtt_connected": 1, "queue_count": 0},
                    {"mqtt_connected": 1, "queue_count": 0, "boot_id": "boot-2", "seq": 7},
                ])

            def __call__(self) -> dict:
                return next(self.values)

        result = driver.observe_stable_empty_queue(Samples(), timeout=1, interval=0, consecutive=3)
        self.assertEqual((result["queue_count"], result["boot_id"], result["seq"]), (0, "boot-2", 7))

    def test_startup_status_allows_boot_event_backlog_until_gateway_connects(self) -> None:
        status = {"device_id": "node1", "boot_id": "boot-1", "queue_count": 1,
                  "mqtt_connected": 0, "seq": 1}
        self.assertEqual(driver.validate_startup_status(status), 1)
        with self.assertRaisesRegex(AssertionError, "wrong firmware device_id"):
            driver.validate_startup_status({**status, "device_id": "node2"})
        with self.assertRaisesRegex(AssertionError, "negative startup"):
            driver.validate_startup_status({**status, "queue_count": -1})

    def test_baseline_fails_when_queue_never_drains(self) -> None:
        with self.assertRaisesRegex(TimeoutError, "stable empty baseline"):
            driver.observe_stable_empty_queue(
                lambda: {"mqtt_connected": 1, "queue_count": 1},
                timeout=0.01, interval=0, consecutive=3,
            )

    def test_lost_application_ack_injection_is_one_shot(self) -> None:
        class FakeBroker:
            def __init__(self) -> None:
                pass

        class FakeHarness:
            MiniBroker = FakeBroker

        class FakeLog:
            def write(self, *_args, **_kwargs) -> None:
                pass

        controller = driver.RuntimeBroker(
            1884, Path("unused.sqlite3"), FakeLog(), FakeHarness
        )
        controller.arm_drop_next_ack()
        self.assertTrue(controller.consume_drop_ack())
        self.assertFalse(controller.consume_drop_ack())


if __name__ == "__main__":
    unittest.main()
