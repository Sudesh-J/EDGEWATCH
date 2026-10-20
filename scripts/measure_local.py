"""Run a small local measurement and write results/latest_run.json.

The MQTT portion uses the real simulators, broker, and gateway.
Azure sends are forced to mock mode so this script does not publish to IoT Hub.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.azure_sender import MockAzureSender
from gateway.gateway import handle_raw_message, payload_size
from gateway.normalizer import normalize_message
from gateway.state_store import StateStore

RESULTS = ROOT / "results"
TIMESTAMP = "2026-01-01T00:00:00.000Z"


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def in_process_run() -> dict:
    store = StateStore(path=None)
    sender = MockAzureSender()
    samples = [
        ("devices/contiki-01/telemetry", {"node_id": "contiki-01", "temp_c": 28.4, "battery_mv": 2950}),
        ("devices/freertos-01/telemetry", {"deviceId": "freertos-01", "sensor": "humidity", "value": 64.0, "unit": "%"}),
        ("devices/airquality-01/telemetry", {"sensorId": "airquality-01", "pm25": 12.5, "pm10": 22.0}),
    ]
    sizes = {}
    for topic, native in samples:
        normalized = normalize_message(native, timestamp=TIMESTAMP)
        sizes[normalized["device_id"]] = payload_size(normalized)
        handle_raw_message(store, sender, topic, json.dumps(native))
    handle_raw_message(store, sender, "devices/bad-01/telemetry", "{not-json")
    handle_raw_message(store, sender, "devices/contiki-01/telemetry", json.dumps({"node_id": "contiki-01", "temp_c": 21}))
    failing = StateStore(path=None)
    handle_raw_message(
        failing,
        MockAzureSender(fail=True),
        "devices/freertos-01/telemetry",
        json.dumps({"deviceId": "freertos-01", "sensor": "humidity", "value": 40, "unit": "%"}),
    )
    state = store.snapshot()
    failed = failing.snapshot()
    return {
        "description": "Direct function calls. No MQTT broker and no Azure IoT Hub.",
        "messages_handled": 5,
        "invalid_messages": state["counters"]["invalid"],
        "normalized": state["counters"]["normalized"],
        "mock_success": state["counters"]["azure_success"],
        "mock_failed_in_separate_check": failed["counters"]["azure_failed"],
        "compact_payload_bytes": sizes,
        "processing_ms": state["latency"]["processing_samples_ms"],
        "forward_ms": state["latency"]["forward_samples_ms"],
        "avg_processing_ms": state["latency"]["avg_processing_ms"],
        "avg_forward_ms": state["latency"]["avg_forward_ms"],
    }


def _spawn(args, log_path: Path, env):
    handle = log_path.open("w", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    process = subprocess.Popen(
        args,
        cwd=ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    process._edgewatch_log = handle
    return process


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    process._edgewatch_log.close()


def mqtt_run() -> dict:
    host = "127.0.0.1"
    port = 1883
    if not port_open(host, port):
        return {"skipped": True, "reason": f"No MQTT broker accepted a connection on {host}:{port}."}

    data_dir = RESULTS / "mqtt_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AZURE_MODE"] = "mock"
    env["IOTHUB_DEVICE_CONNECTION_STRING"] = ""
    env["EDGEWATCH_DATA_DIR"] = str(data_dir)
    env["MQTT_HOST"] = host
    env["MQTT_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"

    gateway = _spawn([sys.executable, str(ROOT / "gateway" / "gateway.py")], RESULTS / "gateway.log", env)
    time.sleep(1.5)
    device_cmds = [
        [sys.executable, str(ROOT / "devices" / "contiki_sim.py"), "--count", "3", "--interval", "1", "--seed", "1"],
        [sys.executable, str(ROOT / "devices" / "freertos_sim.py"), "--count", "3", "--interval", "1", "--seed", "1"],
        [sys.executable, str(ROOT / "devices" / "airquality_sim.py"), "--count", "3", "--interval", "1", "--seed", "1"],
    ]
    devices = []
    for index, cmd in enumerate(device_cmds):
        devices.append(_spawn(cmd, RESULTS / f"device_{index}.log", env))

    import paho.mqtt.client as mqtt

    invalid_published = 0
    client = mqtt.Client(client_id="edgewatch-measure")
    client.connect(host, port, keepalive=20)
    client.loop_start()
    for topic, body in (
        ("devices/bad-01/telemetry", "{not-json"),
        ("devices/contiki-01/telemetry", json.dumps({"node_id": "contiki-01", "temp_c": 22.0})),
    ):
        info = client.publish(topic, body, qos=1)
        info.wait_for_publish(timeout=3)
        invalid_published += 1
    client.loop_stop()
    client.disconnect()

    deadline = time.time() + 25
    state = {}
    while time.time() < deadline:
        for process in devices:
            process.wait(timeout=0.1) if process.poll() is not None else None
        state_path = data_dir / "live_state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                state = {}
            if state.get("counters", {}).get("received", 0) >= 9 + invalid_published and all(
                process.poll() is not None for process in devices
            ):
                break
        time.sleep(0.4)

    for process in devices:
        _stop(process)
    time.sleep(0.4)
    state_path = data_dir / "live_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    _stop(gateway)

    device_exit = [process.returncode for process in devices]
    return {
        "skipped": False,
        "azure_mode_forced": "mock",
        "messages_requested_from_simulators": 9,
        "invalid_messages_published": invalid_published,
        "device_exit_codes": device_exit,
        "counters": state.get("counters"),
        "latency": {
            "avg_processing_ms": state.get("latency", {}).get("avg_processing_ms"),
            "avg_forward_ms": state.get("latency", {}).get("avg_forward_ms"),
            "last_processing_ms": state.get("latency", {}).get("last_processing_ms"),
            "last_forward_ms": state.get("latency", {}).get("last_forward_ms"),
            "processing_samples_ms": state.get("latency", {}).get("processing_samples_ms"),
            "forward_samples_ms": state.get("latency", {}).get("forward_samples_ms"),
            "min_payload_bytes": state.get("latency", {}).get("min_payload_bytes"),
            "max_payload_bytes": state.get("latency", {}).get("max_payload_bytes"),
            "last_payload_bytes": state.get("latency", {}).get("last_payload_bytes"),
        },
        "devices_seen": sorted((state.get("devices") or {}).keys()),
        "definitions": {
            "processing": state.get("latency", {}).get("processing_definition"),
            "forward": state.get("latency", {}).get("forward_definition"),
        },
    }


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    report = {
        "in_process": in_process_run(),
        "mqtt_integration": mqtt_run(),
        "azure_portal_verified": False,
        "note": "Mock forwarding is not Azure delivery. azure_portal_verified is false unless a person confirms the message in IoT Hub tools.",
    }
    destination = RESULTS / "latest_run.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(destination)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
