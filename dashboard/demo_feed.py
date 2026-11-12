"""Optional dashboard-only synthetic feed; never uses gas or Wokwi."""
import json
import math
import time

import server

server.init_db()
boot = f"dashboard-demo-{int(time.time())}"
seq = 0
print("Synthetic dashboard feed running (Ctrl+C to stop). All readings are simulated.", flush=True)
while True:
    phase = seq / 4
    for device in ("node1", "node2", "node3"):
        seq += 1
        lpg = 8 + 8 * max(0, math.sin(phase / 4)) if device == "node2" else 4 + 2 * math.sin(phase)
        co = 7 + 9 * max(0, math.sin(phase / 5)) if device == "node3" else 4 + math.sin(phase / 2)
        lpg_state = "WATCH" if lpg >= 12 else "SAFE"
        co_state = "WATCH" if co >= 12 else "SAFE"
        channel = lambda signal, state: {"state": state, "signal_pct": round(signal, 1),
                                          "baseline_pct": 4, "rise_pct": round(signal - 4, 1), "valid": True}
        data = {"schema": 2, "kind": "telemetry", "home_id": server.HOME_ID, "device_id": device,
                "boot_id": boot, "seq": seq, "uptime_ms": int(phase * 1000),
                "state": max((lpg_state, co_state), key=server.SEVERITY.get), "changed_gas": "NONE",
                "lpg": channel(lpg, lpg_state), "co": channel(co, co_state),
                "temperature_c": round(28 + math.sin(phase / 5), 1),
                "humidity_pct": 62, "valve_open_sim": True, "stove_on_sim": device != "node1",
                "simulated": True}
        server.ingest(f"home/{server.HOME_ID}/device/{device}/telemetry", json.dumps(data).encode())
    time.sleep(2)
