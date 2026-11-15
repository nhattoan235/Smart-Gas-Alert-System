"""Static validation for Wokwi scenario YAML and linked node diagrams.

This does not run a Wokwi simulation or verify physical hardware behavior.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SERIAL_TRANSITION_FORMAT = 'Serial.printf("%s: LPG %s %.1f%%, CO %s %.1f%%'
VALID_STATES = {"WARMUP", "SAFE", "WATCH", "WARNING", "DANGER", "FAULT"}
SUPPORTED_CONTROLS = {
    "lpg": {"position"}, "co": {"position"}, "temp": {"position"},
    "humid": {"position"}, "interference": {"position"},
    "lpgFault": {"pressed"}, "coFault": {"pressed"},
    "ack": {"pressed"}, "inspect": {"pressed"},
}


def validate_scenarios(root: Path = ROOT) -> tuple[int, list[str]]:
    root = Path(root)
    firmware_root = root / "firmware"
    firmware_source = (firmware_root / "src" / "main.cpp").read_text(encoding="utf-8")
    assertions = 0
    summaries: list[str] = []
    assert SERIAL_TRANSITION_FORMAT in firmware_source, "scenario serial assertions need updating for current firmware format"
    for node in ("node1", "node2", "node3"):
        sim = firmware_root / "sim" / node
        diagram = json.loads((sim / "diagram.json").read_text(encoding="utf-8"))
        config = tomllib.loads((sim / "wokwi.toml").read_text(encoding="utf-8"))
        assert config.get("wokwi", {}).get("version") == 1, f"{node}: unsupported Wokwi config version"
        for artifact_key, filename in (("firmware", "firmware.bin"), ("elf", "firmware.elf")):
            configured = config.get("wokwi", {}).get(artifact_key)
            assert isinstance(configured, str) and configured.strip(), f"{node}: missing configured {artifact_key} path"
            artifact = (sim / configured).resolve(strict=False)
            expected = (firmware_root / ".pio" / "build" / node / filename).resolve(strict=False)
            assert artifact == expected, f"{node}: configured {artifact_key} path must resolve to this node's PlatformIO output: {configured}"
        assert diagram["version"] == 1
        parts = {part["id"] for part in diagram["parts"]}
        wires = {endpoint for wire in diagram["connections"] for endpoint in wire[:2]}
        scenario_paths = sorted((sim / "scenarios").glob("*.yaml"))
        assert scenario_paths, f"{node}: no scenario YAML files"
        for scenario_path in scenario_paths:
            scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
            assert scenario["version"] == 1, f"{scenario_path}: unsupported scenario version"
            assert isinstance(scenario["steps"], list) and scenario["steps"], f"{scenario_path}: no steps"
            for index, step in enumerate(scenario["steps"], start=1):
                assert isinstance(step, dict) and len(step) == 1, f"{scenario_path}: step {index} must have one action"
                action, value = next(iter(step.items()))
                if action == "set-control":
                    part_id = value["part-id"]
                    control = value["name"]
                    assert part_id in parts, f"{scenario_path}: missing part {part_id}"
                    assert control in SUPPORTED_CONTROLS.get(part_id, set()), (
                        f"{scenario_path}: unsupported control {part_id}.{control}"
                    )
                    if control == "position":
                        assert 0 <= float(value["value"]) <= 1, f"{scenario_path}: pot out of range"
                    assertions += 1
                elif action == "expect-pin":
                    part_id = value["part-id"]
                    pin = str(value["name"])
                    assert part_id in parts, f"{scenario_path}: missing pin part {part_id}"
                    assert f"{part_id}:{pin}" in wires, f"{scenario_path}: pin {part_id}:{pin} is not in diagram wiring"
                    assert value["value"] in (0, 1), f"{scenario_path}: digital pin value must be 0 or 1"
                    assertions += 1
                elif action == "wait-serial":
                    assert isinstance(value, str) and value.strip(), f"{scenario_path}: empty serial assertion"
                    if value.startswith(f"{node}:"):
                        assert value.startswith(f"{node}: LPG "), (
                            f"{scenario_path}: firmware prefixes transition lines with '{node}: LPG'; got {value!r}"
                        )
                        state = value.split()[2]
                        assert state in VALID_STATES, f"{scenario_path}: unknown LPG serial state {state}"
                    elif value.startswith("CO "):
                        state = value.split()[1]
                        assert state in VALID_STATES, f"{scenario_path}: unknown CO serial state {state}"
                    else:
                        raise AssertionError(f"{scenario_path}: assertion is not present in transition line format: {value!r}")
                    assertions += 1
                elif action == "delay":
                    assert isinstance(value, str) and value.endswith(("ms", "s")), f"{scenario_path}: delay needs units"
                else:
                    raise AssertionError(f"{scenario_path}: unsupported action {action}")
            assert any("expect-pin" in step for step in scenario["steps"]), (
                f"{scenario_path}: no GPIO assertion"
            )
            summaries.append(
                f"PASS static: {node} {scenario_path.name} TOML output paths, diagram, YAML, {len(scenario['steps'])} steps"
            )
    summaries.append(f"PASS static: {assertions} serial/GPIO/control assertions checked")
    summaries.append("SCOPE: static only; artifact existence is checked separately after a fresh build")
    return assertions, summaries


def main() -> int:
    _, summaries = validate_scenarios()
    for summary in summaries:
        print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
