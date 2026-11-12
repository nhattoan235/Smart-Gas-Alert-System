"""Generate a build-local MQTT password header from the environment."""

import json
import os
from pathlib import Path

Import("env")

generated = Path(env.subst("$BUILD_DIR")) / "generated"
generated.mkdir(parents=True, exist_ok=True)
header = generated / "MqttCredentials.h"
password = os.environ.get("GAS_MQTT_PASSWORD", "")
if not password:
    raise RuntimeError(
        "GAS_MQTT_PASSWORD is empty. Set it in the same PowerShell session before running pio run."
    )
content = "#pragma once\n#define MQTT_PASSWORD " + json.dumps(
    password
) + "\n"
if not header.exists() or header.read_text(encoding="utf-8") != content:
    header.write_text(content, encoding="utf-8")
env.Append(CPPPATH=[str(generated)])
