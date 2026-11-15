"""Generate a build-local MQTT password header from the environment."""

import json
import os
from pathlib import Path

Import("env")

password = os.environ.get("GAS_MQTT_PASSWORD", "")
hardware_target = env.get("PIOENV", "") == "n8r8_hardware"
if not password and not hardware_target:
    raise RuntimeError(
        "GAS_MQTT_PASSWORD is empty. Set it in the same PowerShell session before running pio run."
    )
if not password and hardware_target:
    print("Building the unconfigured N8R8 target with an empty MQTT password; its host and Wi-Fi SSID are placeholders.")
generated = Path(env.subst("$BUILD_DIR")) / "generated"
generated.mkdir(parents=True, exist_ok=True)
header = generated / "MqttCredentials.h"
content = "#pragma once\n#define MQTT_PASSWORD " + json.dumps(
    password
) + "\n"
if not header.exists() or header.read_text(encoding="utf-8") != content:
    header.write_text(content, encoding="utf-8")
env.Append(CPPPATH=[str(generated)])
