"""Assert observed Wokwi digital output activity from a VCD capture."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_wokwi_vcd.py <capture.vcd>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file() or path.stat().st_size == 0:
        raise AssertionError(f"VCD capture missing or empty: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    variables = {}
    for line in text.splitlines():
        match = re.match(r"\s*\$var\s+\S+\s+\d+\s+(\S+)\s+(\S+)", line)
        if match:
            code, name = match.groups()
            variables[name] = code
    required = ("BUZZER", "GREEN", "AMBER", "RED")
    missing = [name for name in required if name not in variables]
    if missing:
        raise AssertionError(f"VCD missing named output channels: {missing}; channels={sorted(variables)}")
    observed = {name: set() for name in required}
    transitions = {name: 0 for name in required}
    last_value = {name: None for name in required}
    codes = {code: name for name, code in variables.items() if name in observed}
    for line in text.splitlines():
        stripped = line.strip()
        scalar = re.fullmatch(r"([01xXzZ])\s*(\S+)", stripped)
        if scalar and scalar.group(2) in codes:
            value, code = scalar.groups()
            name = codes[code]
            value = value.lower()
            observed[name].add(value)
            if last_value[name] is not None and last_value[name] != value:
                transitions[name] += 1
            last_value[name] = value
    for name in required:
        levels = observed[name]
        if not {"0", "1"}.issubset(levels):
            raise AssertionError(f"{name} did not show both digital levels in VCD: {sorted(levels)}")
    if transitions["BUZZER"] < 100:
        raise AssertionError(
            f"BUZZER had only {transitions['BUZZER']} observed digital transitions; expected sustained/pulsed tone activity"
        )
    print("PASS runtime GPIO capture: GREEN, AMBER, RED changed levels; BUZZER exceeded 100 observed edges")
    print("LIMIT: VCD edge count does not certify audible SPL or physical buzzer behavior")
    print(f"evidence={path} bytes={path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
