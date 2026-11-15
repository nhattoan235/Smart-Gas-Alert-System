"""Generate a build-local MQTT password header from the environment."""

import json
import os
from pathlib import Path

Import("env")

password = os.environ.get("GAS_MQTT_PASSWORD", "")
hardware_target = env.get("PIOENV", "") == "n8r8_hardware"
isolated_runtime_target = env.get("PIOENV", "") == "node1_nvs_runtime"
tls_compile_target = env.get("PIOENV", "") == "node1_mqtt_tls_compile"
tls_runtime_target = env.get("PIOENV", "") == "node1_mqtt_tls_runtime"
if not password and not hardware_target and not isolated_runtime_target and not tls_compile_target:
    raise RuntimeError(
        "GAS_MQTT_PASSWORD is empty. Set it in the same PowerShell session before running pio run."
    )
if not password and isolated_runtime_target:
    print("Building the isolated NVS runtime test target with an empty password for its loopback-only broker.")
if not password and tls_compile_target:
    print("Building the TLS compile-only target with no password; this image is not a runnable broker configuration.")
if not password and hardware_target:
    print("Building the unconfigured N8R8 target with an empty MQTT password; its host and Wi-Fi SSID are placeholders.")
ca_cert = ""
if tls_compile_target or tls_runtime_target:
    ca_path = os.environ.get("GAS_MQTT_CA_CERT_FILE", "")
    if not ca_path:
        raise RuntimeError(
            f"GAS_MQTT_CA_CERT_FILE is required for {env.get('PIOENV', 'TLS target')}; provide a PEM CA file."
        )
    try:
        ca_cert = Path(ca_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            "GAS_MQTT_CA_CERT_FILE must point to a readable UTF-8 PEM CA certificate."
        ) from exc
    if "-----BEGIN CERTIFICATE-----" not in ca_cert or "-----END CERTIFICATE-----" not in ca_cert:
        raise RuntimeError("GAS_MQTT_CA_CERT_FILE does not contain a PEM certificate.")
generated = Path(env.subst("$BUILD_DIR")) / "generated"
generated.mkdir(parents=True, exist_ok=True)
header = generated / "MqttCredentials.h"
content = (
    "#pragma once\n#define MQTT_PASSWORD " + json.dumps(password) +
    "\n#define MQTT_CA_CERT " + json.dumps(ca_cert) + "\n"
)
if not header.exists() or header.read_text(encoding="utf-8") != content:
    header.write_text(content, encoding="utf-8")
env.Append(CPPPATH=[str(generated)])
