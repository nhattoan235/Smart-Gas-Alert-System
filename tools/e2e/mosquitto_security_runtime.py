"""Run MQTT auth, ACL and TLS assertions against a real Mosquitto process.

Only loopback and an OS-assigned ephemeral TCP port are used. The adapter never
connects to the demo broker. Test credentials and private keys are ephemeral.
The generated passwords are passed only to the local mosquitto_passwd process
to create a temporary hashed file; credentials and private keys are not logged.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Blocked(RuntimeError):
    pass


def import_security_harness():
    path = ROOT / "tools/e2e/mqtt_security_harness.py"
    spec = importlib.util.spec_from_file_location("mqtt_security_harness_mosquitto", path)
    if spec is None or spec.loader is None:
        raise Blocked("MQTT security client helpers are unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def quote_config(path: Path) -> str:
    # Mosquitto's Windows config parser treats quotes and doubled backslashes
    # as literal path characters. Test paths are kept in a no-space workspace.
    return str(path)


def build_password_file(passwd_exe: Path, password_file: Path,
                        users: dict[str, str]) -> None:
    first = True
    for username, password in users.items():
        # Windows mosquitto_passwd uses a console prompt for interactive mode;
        # redirected stdin is not accepted by the official executable. The
        # credentials here are generated per run and never written to logs.
        args = [str(passwd_exe), "-b"]
        if first:
            args.append("-c")
        args.extend([str(password_file), username, password])
        try:
            completed = subprocess.run(
                args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise Blocked("mosquitto_passwd timed out creating the temporary hashed credentials") from exc
        if completed.returncode != 0:
            raise Blocked(
                f"mosquitto_passwd exited {completed.returncode} creating temporary hashed credentials; no broker was started"
            )
        first = False
    contents = password_file.read_text(encoding="utf-8", errors="strict")
    if any(password in contents for password in users.values()):
        raise AssertionError("mosquitto_passwd did not hash a temporary password")
    if not all(username + ":" in contents for username in users):
        raise AssertionError("mosquitto_passwd file is missing one or more test accounts")


def wait_ready(process: subprocess.Popen, port: int, timeout: float, log_path: Path) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            tail = "\n".join(text.splitlines()[-20:]) or "broker log file was not created"
            raise AssertionError(f"Mosquitto exited before opening its isolated listener (exit={process.returncode}).\n{tail}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
    raise Blocked(f"Mosquitto test listener did not become ready within {timeout:g}s: {last_error}")


def assert_tls_rejection(port: int, context: ssl.SSLContext, server_name: str) -> None:
    raw = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        context.wrap_socket(raw, server_hostname=server_name)
    except ssl.SSLCertVerificationError:
        return
    raise AssertionError(f"Mosquitto TLS accepted invalid certificate identity {server_name!r}")


def run(args: argparse.Namespace) -> int:
    import secrets
    import json
    from datetime import datetime, timezone

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    live_log = output / "mqtt-security-mosquitto-live.jsonl"

    def record(event: str, **values) -> None:
        row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event, **values}
        encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
        with live_log.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        print(encoded, flush=True)

    mosquitto = Path(args.mosquitto_exe)
    passwd = Path(args.mosquitto_passwd_exe)
    if not mosquitto.is_file() or not passwd.is_file():
        record("RESULT", status="BLOCKED", detail="real mosquitto.exe and mosquitto_passwd.exe are both required")
        return 77

    security = import_security_harness()
    credentials = {
        "node1-test": secrets.token_urlsafe(32),
        "gateway-test": secrets.token_urlsafe(32),
    }
    configured_temp_root = os.environ.get("GAS_E2E_TMPDIR")
    temp_root = Path(configured_temp_root).resolve() if configured_temp_root else output / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    process = None
    broker_log_copy = output / "mosquitto-broker.log"
    expired_broker_log_copy = output / "mosquitto-expired-broker.log"
    console_log_copy = output / "mosquitto-console.log"
    expired_console_log_copy = output / "mosquitto-expired-console.log"
    with tempfile.TemporaryDirectory(prefix="mosquitto-security-", dir=temp_root) as temp:
        work = Path(temp)
        ca, cert, key, other_ca = security.issue_certificates(work)
        password_file = work / "passwd.txt"
        try:
            build_password_file(passwd, password_file, credentials)
        except Blocked as exc:
            record("RESULT", status="BLOCKED", error_type=type(exc).__name__, detail=str(exc))
            return 77
        port = free_loopback_port()
        acl_text = (ROOT / "tools/e2e/fixtures/mqtt-security.acl").read_text(encoding="utf-8")
        acl_file = work / "acl.txt"
        acl_file.write_text(acl_text, encoding="utf-8")
        broker_log = work / "broker.log"
        console_log = work / "console.log"
        config = work / "mosquitto.conf"
        config.write_text("\n".join([
            f"listener {port} 127.0.0.1",
            "allow_anonymous false",
            f"password_file {quote_config(password_file)}",
            f"acl_file {quote_config(acl_file)}",
            f"cafile {quote_config(ca)}",
            f"certfile {quote_config(cert)}",
            f"keyfile {quote_config(key)}",
            "tls_version tlsv1.2",
            "persistence false",
            "log_type all",
            f"log_dest file {quote_config(broker_log)}",
            "",
        ]), encoding="utf-8")
        record("mosquitto_configured", bind="127.0.0.1", port=port,
               tls="TLS 1.2+", auth="required", persistence="false", demo_port_1883="untouched")
        console_stream = console_log.open("wb")
        try:
            process = subprocess.Popen(
                [str(mosquitto), "-c", str(config)],
                stdin=subprocess.DEVNULL, stdout=console_stream, stderr=subprocess.STDOUT,
                cwd=str(work), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record("mosquitto_process_started", pid=process.pid)
            wait_ready(process, port, 12, broker_log)
            trusted = ssl.create_default_context(cafile=str(ca))

            node, rc = security.mqtt_client(port, trusted, "node1-test", credentials["node1-test"])
            if rc != 0:
                raise AssertionError(f"Mosquitto rejected valid node credentials with CONNACK {rc}")
            record("mqtt_valid_credentials", status="PASS", connack=rc)
            own = security.subscribe(node, "home/security-test/device/node1/ack")
            if own not in (0, 1):
                raise AssertionError("Mosquitto denied node subscription to its own application ACK")
            other = security.subscribe(node, "home/security-test/device/node2/ack")
            if other not in (0, 1, 0x80):
                raise AssertionError(f"Mosquitto returned an invalid cross-device ACK SUBACK: {other:#x}")

            gateway, rc = security.mqtt_client(
                port, trusted, "gateway-test", credentials["gateway-test"],
                client_id="gateway-cross-device-probe",
            )
            if rc != 0:
                raise AssertionError(f"Mosquitto rejected valid gateway credentials with CONNACK {rc}")
            cross_device_ack = "home/security-test/device/node2/ack"
            cross_publish = security.publish_qos1(gateway, cross_device_ack, "cross-device-probe")
            if cross_publish != 1:
                raise AssertionError("Mosquitto denied the gateway's permitted cross-device ACK publish")
            previous_timeout = node.gettimeout()
            node.settimeout(0.5)
            try:
                leaked = security.receive_publish(node)
            except TimeoutError:
                leaked = None
            finally:
                node.settimeout(previous_timeout)
            if leaked is not None:
                raise AssertionError(
                    f"Mosquitto delivered a cross-device ACK to node1 despite its read ACL: {leaked!r}"
                )
            allowed = security.publish_qos1(node, "home/security-test/device/node1/event", "{}")
            if allowed != 1:
                raise AssertionError("Mosquitto ACL denied the node's own event publish")
            forbidden = security.publish_qos1(node, "home/security-test/device/node1/ack", "forged:1")
            node.close()
            time.sleep(0.25)
            broker_text = broker_log.read_text(encoding="utf-8", errors="replace") if broker_log.exists() else ""
            if forbidden != -1 and not __import__("re").search(r"(?i)denied\s+PUBLISH[^\r\n]*node1/ack", broker_text):
                raise AssertionError("Mosquitto did not demonstrate a denied forged ACK publish in client result or broker log")
            if __import__("re").search(r"(?i)Received PUBLISH[^\r\n]*node1/ack", broker_text):
                raise AssertionError("Mosquitto log indicates the node's forged ACK publish was accepted")
            record("mosquitto_node_acl", status="PASS", own_event_publish=allowed,
                   own_ack_subscribe=own, other_ack_subscribe=other,
                   cross_device_ack_delivered=False, forged_ack_denied=True)

            event_grant = security.subscribe(gateway, "home/security-test/device/+/event")
            if event_grant not in (0, 1):
                raise AssertionError("Mosquitto denied gateway event wildcard subscription")
            ack = security.publish_qos1(gateway, "home/security-test/device/node1/ack", "boot-test:1")
            if ack != 1:
                raise AssertionError("Mosquitto denied gateway application ACK publish")
            gateway.close()
            record("mosquitto_gateway_acl", status="PASS", event_subscription=event_grant, ack_publish=ack)

            for user, password in (("node1-test", "invalid-test-password"), ("", "")):
                client, auth_rc = security.mqtt_client(port, trusted, user, password)
                client.close()
                if auth_rc != 5:
                    raise AssertionError(f"Mosquitto accepted invalid/anonymous credentials (CONNACK {auth_rc})")
            record("mosquitto_authentication", status="PASS", invalid_password="rejected", anonymous="rejected")

            event_topic = "home/security-test/device/node1/event"
            telemetry_topic = "home/security-test/device/node1/telemetry"
            availability_topic = "home/security-test/device/node1/availability"
            observer, rc = security.mqtt_client(
                port, trusted, "gateway-test", credentials["gateway-test"],
                client_id="gateway-semantics-observer",
            )
            if rc != 0:
                raise AssertionError("Mosquitto rejected the semantics observer credentials")
            event_grant = security.subscribe(observer, "home/security-test/device/+/event", qos=1)
            if event_grant not in (0, 1):
                raise AssertionError("Mosquitto denied the semantics observer event subscription")

            node, rc = security.mqtt_client(
                port, trusted, "node1-test", credentials["node1-test"],
                client_id="node1-qos-test",
            )
            if rc != 0:
                raise AssertionError("Mosquitto rejected the QoS test node credentials")
            qos0_sent = security.publish(node, event_topic, "qos0-probe", qos=0)
            qos0_message = security.receive_publish(observer)
            if qos0_sent != 0 or qos0_message["qos"] != 0 or qos0_message["payload"] != "qos0-probe":
                raise AssertionError("Mosquitto QoS 0 publish/delivery did not match expectations")
            qos1_ack = security.publish(node, event_topic, "qos1-probe", qos=1)
            qos1_message = security.receive_publish(observer)
            if qos1_ack != 1 or qos1_message["qos"] != 1 or qos1_message["payload"] != "qos1-probe":
                raise AssertionError("Mosquitto QoS 1 publish/PUBACK/delivery did not match expectations")

            retained_ack = security.publish(node, telemetry_topic, "retained-probe", qos=1, retain=True)
            if retained_ack != 1:
                raise AssertionError("Mosquitto did not PUBACK a retained QoS 1 publish")
            telemetry_grant = security.subscribe(observer, telemetry_topic, qos=1, packet_id=12)
            if telemetry_grant not in (0, 1):
                raise AssertionError("Mosquitto denied the permitted retained telemetry subscription")
            retained_message = security.receive_publish(observer)
            if (retained_message["topic"] != telemetry_topic or retained_message["payload"] != "retained-probe"
                    or retained_message["qos"] != 1 or retained_message["retain"] is not True):
                raise AssertionError("Mosquitto did not replay the retained telemetry message with RETAIN set")

            availability_grant = security.subscribe(observer, availability_topic, qos=1, packet_id=13)
            if availability_grant not in (0, 1):
                raise AssertionError("Mosquitto denied the permitted availability subscription")
            will_node, rc = security.mqtt_client(
                port, trusted, "node1-test", credentials["node1-test"],
                client_id="node1-lwt-test", keepalive=15,
                will=(availability_topic, "offline", 1, True),
            )
            if rc != 0:
                raise AssertionError("Mosquitto rejected the LWT test node credentials")
            will_node.close()  # no MQTT DISCONNECT: force the broker to publish the LWT
            live_will = security.receive_publish(observer)
            if live_will["topic"] != availability_topic or live_will["payload"] != "offline" or live_will["qos"] != 1:
                raise AssertionError("Mosquitto did not deliver the QoS 1 Last Will after an ungraceful disconnect")
            security.disconnect(observer)
            replay_observer, rc = security.mqtt_client(
                port, trusted, "gateway-test", credentials["gateway-test"],
                client_id="gateway-retained-observer",
            )
            if rc != 0:
                raise AssertionError("Mosquitto rejected the retained-message observer credentials")
            availability_grant = security.subscribe(replay_observer, availability_topic, qos=1, packet_id=14)
            if availability_grant not in (0, 1):
                raise AssertionError("Mosquitto denied the permitted retained availability subscription")
            retained_will = security.receive_publish(replay_observer)
            if (retained_will["topic"] != availability_topic or retained_will["payload"] != "offline"
                    or retained_will["retain"] is not True):
                raise AssertionError("Mosquitto did not retain and replay the Last Will")

            reconnect_first, rc = security.mqtt_client(
                port, trusted, "node1-test", credentials["node1-test"], client_id="node1-reconnect-test",
            )
            if rc != 0 or security.publish_qos1(reconnect_first, event_topic, "before-reconnect") != 1:
                raise AssertionError("Mosquitto initial reconnect-test session failed")
            security.disconnect(reconnect_first)
            reconnect_second, rc = security.mqtt_client(
                port, trusted, "node1-test", credentials["node1-test"], client_id="node1-reconnect-test",
            )
            if rc != 0 or security.publish_qos1(reconnect_second, event_topic, "after-reconnect") != 1:
                raise AssertionError("Mosquitto did not accept the reconnected MQTT client")
            security.disconnect(reconnect_second)
            security.disconnect(replay_observer)
            security.disconnect(node)
            record("mosquitto_mqtt_semantics", status="PASS", qos0=True, qos1=True,
                   retained_message=True, last_will=True, retained_last_will=True, client_reconnect=True)

            wrong_ca = ssl.create_default_context(cafile=str(other_ca))
            assert_tls_rejection(port, wrong_ca, "localhost")
            wrong_host = ssl.create_default_context(cafile=str(ca))
            assert_tls_rejection(port, wrong_host, "wrong.example.invalid")
            record("mosquitto_tls_verification", status="PASS", untrusted_ca="rejected",
                   wrong_hostname="rejected")

            process.terminate()
            process.wait(timeout=5)
            process = None
            console_stream.close()
            console_stream = None

            expired_log = work / "broker-expired.log"
            expired_console = work / "console-expired.log"
            expired_config = work / "mosquitto-expired.conf"
            expired_config.write_text("\n".join([
                f"listener {port} 127.0.0.1",
                "allow_anonymous false",
                f"password_file {quote_config(password_file)}",
                f"acl_file {quote_config(acl_file)}",
                f"certfile {quote_config(work / 'expired-server.pem')}",
                f"keyfile {quote_config(key)}",
                "tls_version tlsv1.2",
                "persistence false",
                "log_type all",
                f"log_dest file {quote_config(expired_log)}",
                "",
            ]), encoding="utf-8")
            console_stream = expired_console.open("wb")
            process = subprocess.Popen(
                [str(mosquitto), "-c", str(expired_config)],
                stdin=subprocess.DEVNULL, stdout=console_stream, stderr=subprocess.STDOUT,
                cwd=str(work), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record("mosquitto_expired_certificate_process_started", pid=process.pid, port=port)
            wait_ready(process, port, 12, expired_log)
            assert_tls_rejection(port, trusted, "localhost")
            record("mosquitto_tls_expired_certificate", status="PASS", expected_rejection=True)
            process.terminate()
            process.wait(timeout=5)
            process = None
            console_stream.close()
            console_stream = None

            if broker_log.exists():
                shutil.copyfile(broker_log, broker_log_copy)
            if expired_log.exists():
                shutil.copyfile(expired_log, expired_broker_log_copy)
            if console_log.exists():
                shutil.copyfile(console_log, console_log_copy)
            if expired_console.exists():
                shutil.copyfile(expired_console, expired_console_log_copy)
            record("RESULT", status="PASS", detail="real Mosquitto loopback process passed auth, ACL and TLS assertions")
            if broker_log_copy.exists():
                print("MOSQUITTO_LOG_BEGIN", flush=True)
                print(broker_log_copy.read_text(encoding="utf-8", errors="replace"), end="", flush=True)
                print("MOSQUITTO_LOG_END", flush=True)
            return 0
        except Blocked as exc:
            record("RESULT", status="BLOCKED", error_type=type(exc).__name__, detail=str(exc))
            if console_stream and not console_stream.closed:
                console_stream.flush()
                console_stream.close()
            if console_log.exists():
                shutil.copyfile(console_log, console_log_copy)
            if broker_log.exists():
                shutil.copyfile(broker_log, broker_log_copy)
            if "expired_console" in locals() and expired_console.exists():
                shutil.copyfile(expired_console, expired_console_log_copy)
            if "expired_log" in locals() and expired_log.exists():
                shutil.copyfile(expired_log, expired_broker_log_copy)
            return 77
        except Exception as exc:
            record("RESULT", status="FAIL", error_type=type(exc).__name__, detail=str(exc))
            if broker_log.exists():
                shutil.copyfile(broker_log, broker_log_copy)
            if "expired_log" in locals() and expired_log.exists():
                shutil.copyfile(expired_log, expired_broker_log_copy)
            if console_stream and not console_stream.closed:
                console_stream.flush()
                console_stream.close()
            if console_log.exists():
                shutil.copyfile(console_log, console_log_copy)
                print("MOSQUITTO_LOG_BEGIN", flush=True)
                print(console_log_copy.read_text(encoding="utf-8", errors="replace"), end="", flush=True)
                print("MOSQUITTO_LOG_END", flush=True)
            if "expired_console" in locals() and expired_console.exists():
                shutil.copyfile(expired_console, expired_console_log_copy)
            return 1
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if console_stream and not console_stream.closed:
                console_stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mosquitto-exe", required=True)
    parser.add_argument("--mosquitto-passwd-exe", required=True)
    parser.add_argument("--output-dir", required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
