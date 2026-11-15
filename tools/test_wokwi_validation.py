from __future__ import annotations

import shutil
import tempfile
import unittest
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
        for node in NODES:
            source = PROJECT_ROOT / "firmware" / "sim" / node
            target = firmware / "sim" / node
            (target / "scenarios").mkdir(parents=True)
            for name in ("diagram.json", "wokwi.toml"):
                shutil.copy2(source / name, target / name)
            shutil.copy2(source / "scenarios" / f"{node}.yaml", target / "scenarios" / f"{node}.yaml")

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
