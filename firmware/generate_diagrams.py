"""Generate three copies of the same dual-gas ESP32-S3 node for Wokwi."""
import json
from pathlib import Path

ROOT = Path(__file__).parent


def part(kind, ident, top, left, **attrs):
    return {"type": kind, "id": ident, "top": top, "left": left, "attrs": attrs}


def diagram(node):
    parts = [
        part("board-esp32-s3-devkitc-1", "esp", 0, 0, flashSize="8"),
        part("wokwi-text", "title", -190, -300, text=f"{node.upper()}: LPG + CO + environment"),
        part("wokwi-text", "note", -155, -300, text="Relative signals only; NOT ppm / %LEL. Interference knob: center=0"),
    ]
    pots = (("lpg", "1", -75, "LPG signal %", "35"),
            ("co", "2", 25, "CO signal %", "35"),
            ("temp", "8", 125, "Temperature", "460"),
            ("humid", "9", 225, "Humidity", "500"),
            ("interference", "10", 325, "LPG interference +/-15%", "512"))
    wires = [["esp:TX", "$serialMonitor:RX", "", []],
             ["esp:RX", "$serialMonitor:TX", "", []]]
    for ident, gpio, top, label, value in pots:
        parts.append(part("wokwi-text", f"{ident}Label", top - 30, -285, text=label))
        parts.append(part("wokwi-potentiometer", ident, top, -285, value=value))
        wires.extend([[f"{ident}:SIG", f"esp:{gpio}", "green", []],
                      [f"{ident}:VCC", "esp:3V3.1", "red", []],
                      [f"{ident}:GND", "esp:GND.1", "black", []]])
    for ident, gpio, top, color in (("green", "5", -105, "green"),
                                    ("amber", "6", -45, "yellow"),
                                    ("red", "7", 15, "red")):
        parts.append(part("wokwi-led", ident, top, 290, color=color))
        wires.extend([[f"esp:{gpio}", f"{ident}:A", "green", []],
                      [f"{ident}:C", "esp:GND.2", "black", []]])
    parts.append(part("wokwi-buzzer", "buzzer", 85, 280, volume="0.3"))
    wires.extend([["esp:4", "buzzer:2", "green", []],
                  ["buzzer:1", "esp:GND.2", "black", []]])
    for ident, gpio, top, color, label in (("lpgFault", "13", 165, "red", "LPG FAULT"),
                                           ("coFault", "21", 235, "red", "CO FAULT"),
                                           ("ack", "14", 305, "green", "ACK / MUTE"),
                                           ("inspect", "12", 345, "blue", "INSPECTED")):
        parts.append(part("wokwi-pushbutton", ident, top, 275, color=color, label=label))
        wires.extend([[f"{ident}:1.l", f"esp:{gpio}", color, []],
                      [f"{ident}:2.l", "esp:GND.2", "black", []]])
    for ident, gpio, top, label in (("valve", "15", 155, "Valve context"),
                                    ("stove", "16", 240, "Stove context"),
                                    ("lpgTruth", "11", 325, "LPG leak TRUE (test label)"),
                                    ("coTruth", "17", 410, "CO hazard TRUE (test label)")):
        parts.append(part("wokwi-text", f"{ident}Label", top - 25, 525, text=label))
        parts.append(part("wokwi-slide-switch", ident, top, 525))
        wires.extend([[f"{ident}:2", f"esp:{gpio}", "green", []],
                      [f"{ident}:1", "esp:GND.2", "black", []],
                      [f"{ident}:3", "esp:3V3.2", "red", []]])
    return {"version": 1, "author": "Gas Sentinel", "editor": "wokwi",
            "parts": parts, "connections": wires}


for node in ("node1", "node2", "node3"):
    target = ROOT / "sim" / node
    target.mkdir(parents=True, exist_ok=True)
    (target / "diagram.json").write_text(json.dumps(diagram(node), indent=2), encoding="utf-8")
    (target / "wokwi.toml").write_text(
        f'[wokwi]\nversion = 1\nfirmware = "../../.pio/build/{node}/firmware.bin"\nelf = "../../.pio/build/{node}/firmware.elf"\n',
        encoding="utf-8")
