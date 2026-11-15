"""Verify the NVS runtime test's Wokwi GPIO7 red output and GPIO4 buzzer trace."""
from __future__ import annotations

import re
import sys
from pathlib import Path


class VcdAssertionError(AssertionError):
    pass


def inspect_vcd_text(text: str) -> dict[str, object]:
    names: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\s*\$var\s+\S+\s+\d+\s+(\S+)\s+(\S+)", line)
        if match:
            code, name = match.groups()
            names[name.split(".")[-1]] = code
    missing = {"BUZZER", "RED"} - names.keys()
    if missing:
        raise VcdAssertionError(f"VCD is missing required analyzer channels: {sorted(missing)}")

    codes = {code: name for name, code in names.items() if name in {"BUZZER", "RED"}}
    levels = {"BUZZER": set(), "RED": set()}
    transitions = {"BUZZER": 0, "RED": 0}
    buzzer_transitions_while_red = 0
    previous: dict[str, str] = {}
    red_active = "x"
    for line in text.splitlines():
        scalar = re.fullmatch(r"\s*([01xXzZ])\s*(\S+)\s*", line)
        if not scalar or scalar.group(2) not in codes:
            continue
        value, code = scalar.groups()
        name = codes[code]
        value = value.lower()
        levels[name].add(value)
        if name == "RED":
            red_active = value
        if name in previous and previous[name] != value:
            transitions[name] += 1
            if name == "BUZZER" and red_active == "1":
                buzzer_transitions_while_red += 1
        previous[name] = value

    if "1" not in levels["RED"]:
        raise VcdAssertionError("Wokwi VCD never observed GPIO7/RED high during the triggered alarm capture")
    if not {"0", "1"}.issubset(levels["BUZZER"]):
        raise VcdAssertionError(f"Wokwi VCD did not observe buzzer pin levels 0 and 1: {sorted(levels['BUZZER'])}")
    if buzzer_transitions_while_red < 10:
        raise VcdAssertionError(
            f"Wokwi VCD observed only {buzzer_transitions_while_red} buzzer transitions while GPIO7/RED was high; expected at least 10"
        )
    return {"channels": sorted(names), "red_high": True,
            "buzzer_transitions": transitions["BUZZER"],
            "buzzer_transitions_while_red": buzzer_transitions_while_red,
            "red_transitions": transitions["RED"]}


def validate_runtime_vcd_path(path: Path) -> None:
    if path.name != "nvs-runtime-gpio.vcd" or path.parent.name != "nvs-runtime-wokwi":
        raise VcdAssertionError(
            "refusing to treat an unrelated/older VCD as this run's NVS runtime evidence; "
            "expected test-results/<run-id>/nvs-runtime-wokwi/nvs-runtime-gpio.vcd"
        )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_nvs_runtime_vcd.py <capture.vcd>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    validate_runtime_vcd_path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise VcdAssertionError(f"Wokwi runtime VCD missing or empty: {path}")
    evidence = inspect_vcd_text(path.read_text(encoding="utf-8", errors="replace"))
    print("PASS: current-run Wokwi pin trace observed red LED GPIO7 high and buzzer GPIO4 switching while red was high")
    print(f"evidence={path}; bytes={path.stat().st_size}; buzzer_transitions_while_red={evidence['buzzer_transitions_while_red']}")
    print("LIMIT: Wokwi VCD verifies simulated GPIO nets only; it does not certify audible SPL or physical hardware.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VcdAssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
