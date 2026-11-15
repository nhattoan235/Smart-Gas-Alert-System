"""Check ordered state transitions and sample timing in a Wokwi serial log.

These checks are simulator observations, not gas-sensor accuracy or hardware
timing certification.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


EXPECTED = {
    "node1": [
        ("SAFE", "SAFE"), ("WATCH", "SAFE"), ("WARNING", "SAFE"),
        ("DANGER", "SAFE"), ("WATCH", "SAFE"), ("SAFE", "SAFE"),
    ],
    "node2": [("SAFE", "SAFE"), ("FAULT", "SAFE"), ("WATCH", "SAFE"), ("SAFE", "SAFE")],
    "node3": [
        ("SAFE", "SAFE"), ("SAFE", "WARNING"), ("SAFE", "DANGER"),
        ("SAFE", "WATCH"), ("SAFE", "SAFE"),
    ],
}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in EXPECTED:
        print("usage: verify_wokwi_serial.py <node1|node2|node3> <serial.log>", file=sys.stderr)
        return 2
    node, path_arg = sys.argv[1], sys.argv[2]
    path = Path(path_arg)
    if not path.is_file():
        raise AssertionError(f"serial log missing: {path}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    index = 0
    matched = []
    for lpg_state, co_state in EXPECTED[node]:
        expression = re.compile(
            rf"{re.escape(node)}: LPG {lpg_state} \d+(?:\.\d+)?%, CO {co_state} \d+(?:\.\d+)?%"
        )
        found = next((i for i in range(index, len(lines)) if expression.search(lines[i])), None)
        if found is None:
            raise AssertionError(f"missing ordered transition {node}: LPG {lpg_state}, CO {co_state}")
        matched.append((lpg_state, co_state, found + 1))
        index = found + 1

    timing = [line for line in lines if line.startswith("timing: ")]
    if not timing:
        raise AssertionError("no timing metrics line observed in Wokwi serial output")
    parsed = []
    for line in timing:
        match = re.search(
            r"max sample gap=(\d+) us, max acquire-to-localAlarm=(\d+) us, "
            r"max sampled-condition-to-localAlarm=(\d+) us, missed periods=(\d+)",
            line,
        )
        if not match:
            raise AssertionError(f"unrecognized timing metrics line: {line}")
        parsed.append(tuple(map(int, match.groups())))
    print(f"PASS runtime serial states: {node} ordered transition assertions={len(matched)}")
    print(f"PASS runtime timing observation: samples={len(parsed)} rows max_gap_us={max(x[0] for x in parsed)} missed_periods={sum(x[3] for x in parsed)}")
    print("LIMIT: Wokwi-only timing observation; no physical sensor accuracy or hardware latency claim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
