"""Generate build-local MQTT credentials and connection macros from the environment."""

import json
import os
from pathlib import Path

Import("env")

# Các node chính lấy địa chỉ broker từ .env do tools/start-local.ps1 nạp.
# Giá trị mặc định giữ cấu hình broker local của VM khi build thủ công.
node_targets = {"node1", "node2", "node3"}
if env.get("PIOENV", "") in node_targets:
    mqtt_host = os.environ.get("MQTT_HOST", "192.168.22.128").strip()
    mqtt_user = os.environ.get("MQTT_USER", "iotlab").strip()
    try:
        mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))
    except ValueError as exc:
        raise RuntimeError("MQTT_PORT must be an integer from 1 to 65535.") from exc
    if not mqtt_host:
        raise RuntimeError("MQTT_HOST must not be empty for node1-node3 builds.")
    if not 1 <= mqtt_port <= 65535:
        raise RuntimeError("MQTT_PORT must be an integer from 1 to 65535.")
    env.Append(CPPDEFINES=[
        ("MQTT_HOST", env.StringifyMacro(mqtt_host)),
        ("MQTT_PORT", mqtt_port),
        ("MQTT_USER", env.StringifyMacro(mqtt_user)),
    ])

# Không lưu mật khẩu trong platformio.ini; chỉ đọc từ biến môi trường của lần build này.
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
# Header chứa thông tin xác thực nằm trong thư mục build .pio, không phải source để commit.
header = generated / "MqttCredentials.h"
content = (
    "#pragma once\n#define MQTT_PASSWORD " + json.dumps(password) +
    "\n#define MQTT_CA_CERT " + json.dumps(ca_cert) + "\n"
)
if not header.exists() or header.read_text(encoding="utf-8") != content:
    header.write_text(content, encoding="utf-8")
env.Append(CPPPATH=[str(generated)])
