from __future__ import annotations

import shutil
import tempfile
import unittest
import configparser
from pathlib import Path

from tools.validate_wokwi_artifacts import ArtifactValidationError, validate_artifacts
from tools.validate_wokwi_scenarios import validate_scenarios


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODES = ("node1", "node2", "node3")


class WokwiArtifactValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = PROJECT_ROOT / "test-results"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(prefix="gas-wokwi-validator-", dir=temp_root)
        self.addCleanup(self.temp_dir.cleanup)
        self.project = Path(self.temp_dir.name)
        firmware = self.project / "firmware"
        (firmware / "src").mkdir(parents=True)
        shutil.copy2(PROJECT_ROOT / "firmware" / "src" / "main.cpp", firmware / "src" / "main.cpp")
        shutil.copy2(PROJECT_ROOT / "firmware" / "platformio.ini", firmware / "platformio.ini")
        for node in NODES:
            source = PROJECT_ROOT / "firmware" / "sim" / node
            target = firmware / "sim" / node
            (target / "scenarios").mkdir(parents=True)
            for name in ("diagram.json", "wokwi.toml"):
                shutil.copy2(source / name, target / name)
            for scenario in (source / "scenarios").glob("*.yaml"):
                shutil.copy2(scenario, target / "scenarios" / scenario.name)

    def _create_artifacts(self) -> None:
        build_root = self.project / "firmware" / ".pio" / "build"
        for node in NODES:
            node_dir = build_root / node
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "firmware.bin").write_bytes(f"{node}-firmware".encode())
            (node_dir / "firmware.elf").write_bytes(f"{node}-elf".encode())

    def test_static_validation_passes_when_artifacts_are_absent(self) -> None:
        self.assertFalse((self.project / "firmware" / ".pio").exists())
        assertions, summaries = validate_scenarios(self.project)
        self.assertGreater(assertions, 0)
        self.assertTrue(any("static only" in line for line in summaries))

    def test_static_validation_passes_when_artifacts_are_present(self) -> None:
        self._create_artifacts()
        assertions, _ = validate_scenarios(self.project)
        self.assertGreater(assertions, 0)

    def test_additional_fault_scenario_is_included_in_static_validation(self) -> None:
        assertions, summaries = validate_scenarios(self.project)
        self.assertGreater(assertions, 56)
        self.assertTrue(any("sensor-faults.yaml" in line for line in summaries))

    def test_flame_monitor_is_enabled_only_for_node1_profiles(self) -> None:
        config = configparser.ConfigParser(interpolation=None)
        config.read(self.project / "firmware" / "platformio.ini", encoding="utf-8")
        for profile in (
            "env:node1",
            "env:node1_nvs_runtime",
            "env:node1_mqtt_tls_compile",
            "env:node1_mqtt_tls_runtime",
        ):
            with self.subTest(profile=profile):
                self.assertIn("-D FLAME_MONITOR_ENABLED=1", config[profile]["build_flags"])
        for profile in ("env:node2", "env:node3", "env:n8r8_hardware"):
            with self.subTest(profile=profile):
                self.assertNotIn("FLAME_MONITOR_ENABLED=1", config[profile]["build_flags"])

    def test_artifact_validation_fails_when_outputs_are_absent(self) -> None:
        with self.assertRaisesRegex(ArtifactValidationError, "artifact is missing"):
            validate_artifacts(self.project / "firmware")

    def test_artifact_validation_passes_for_nonempty_outputs(self) -> None:
        self._create_artifacts()
        artifacts = validate_artifacts(self.project / "firmware")
        self.assertEqual(len(artifacts), 6)
        self.assertTrue(all(item["size_bytes"] > 0 and item["sha256"] for item in artifacts))


if __name__ == "__main__":
    unittest.main()
