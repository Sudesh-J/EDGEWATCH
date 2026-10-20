"""MQTT subscriber that normalizes device telemetry and forwards it.

Run from the project root:

    python gateway/gateway.py
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.azure_sender import create_sender
from gateway.normalizer import NormalizationError, normalize_message
from gateway.state_store import StateStore

TOPIC = "devices/+/telemetry"
LOG = logging.getLogger("edgewatch.gateway")


def payload_size(message: dict) -> int:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    return len(body.encode("utf-8"))


def handle_raw_message(store: StateStore, sender, topic: str, raw) -> None:
    """Parse, validate, normalize, store, and forward one MQTT payload."""
    started = time.perf_counter()
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = raw
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        store.record_invalid(topic, "malformed JSON")
        LOG.warning("invalid payload on %s: malformed JSON", topic)
        return
    try:
        normalized = normalize_message(payload)
    except NormalizationError as exc:
        store.record_invalid(topic, str(exc))
        LOG.warning("invalid payload on %s: %s", topic, exc)
        return

    processing_ms = (time.perf_counter() - started) * 1000
    store.record_normalized(normalized, processing_ms, topic)
    LOG.info(
        "normalized %s processing_ms=%.3f",
        json.dumps(normalized, ensure_ascii=False),
        processing_ms,
    )

    forward_started = time.perf_counter()
    try:
        result = sender.send(normalized)
    except Exception as exc:
        from gateway.azure_sender import SendResult, redact

        result = SendResult(False, getattr(sender, "mode", "unknown"), redact(f"{type(exc).__name__}: {exc}"))
    forward_ms = (time.perf_counter() - forward_started) * 1000
    store.record_forward(normalized["device_id"], result, forward_ms, payload_size(normalized))
    if result.ok and result.mode == "mock":
        LOG.info(
            "mock send stored locally for %s forward_ms=%.3f (not delivered to Azure)",
            normalized["device_id"],
            forward_ms,
        )
    elif result.ok:
        LOG.info(
            "IoT Hub SDK send returned for %s forward_ms=%.3f — confirm in Azure Portal",
            normalized["device_id"],
            forward_ms,
        )
    else:
        LOG.warning(
            "send failed for %s mode=%s error=%s",
            normalized["device_id"],
            result.mode,
            result.error,
        )


def load_environment() -> None:
    load_dotenv(ROOT / ".env")


def read_settings() -> dict:
    data_dir = Path(os.getenv("EDGEWATCH_DATA_DIR", ROOT / "data"))
    return {
        "host": os.getenv("MQTT_HOST", "127.0.0.1"),
        "port": int(os.getenv("MQTT_PORT", "1883")),
        "state_path": data_dir / "live_state.json",
        "log_path": data_dir / "events.jsonl",
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_environment()
    settings = read_settings()
    sender, azure_info = create_sender()
    if settings["log_path"].exists():
        settings["log_path"].write_text("", encoding="utf-8")
    store = StateStore(settings["state_path"], settings["log_path"])
    store.configure(settings["host"], settings["port"], azure_info)

    LOG.info("EDGEWATCH gateway starting")
    LOG.info("MQTT broker %s:%s topic %s", settings["host"], settings["port"], TOPIC)
    LOG.info("Azure mode=%s delivery=%s", azure_info["mode"], azure_info["delivery"])
    if azure_info.get("warning"):
        LOG.warning("%s", azure_info["warning"])
    LOG.info("%s", azure_info["note"])

    stop_event = threading.Event()

    def request_stop(signum, frame):
        LOG.info("stop requested")
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    client = mqtt.Client(client_id="edgewatch-gateway")
    client.user_data_set({"store": store, "sender": sender})
    client.reconnect_delay_set(min_delay=1, max_delay=15)

    def on_connect(mqtt_client, userdata, flags, rc):
        if rc != 0:
            userdata["store"].set_mqtt(False, f"connect refused: {rc}")
            LOG.warning("MQTT connect refused: %s", rc)
            return
        mqtt_client.subscribe(TOPIC, qos=1)
        userdata["store"].set_mqtt(True, None)
        LOG.info("MQTT connected and subscribed to %s", TOPIC)

    def on_disconnect(mqtt_client, userdata, rc):
        if stop_event.is_set():
            return
        userdata["store"].set_mqtt(False, "broker connection lost")
        LOG.warning("MQTT disconnected (rc=%s). Client will retry.", rc)

    def on_message(mqtt_client, userdata, message):
        try:
            handle_raw_message(userdata["store"], userdata["sender"], message.topic, message.payload)
        except Exception:
            LOG.exception("unhandled error while processing %s", message.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    connected_once = False
    while not stop_event.is_set():
        try:
            client.connect(settings["host"], settings["port"], keepalive=60)
            client.loop_start()
            connected_once = True
            break
        except Exception as exc:
            store.set_mqtt(False, "broker unavailable")
            LOG.warning(
                "MQTT broker unavailable at %s:%s (%s). Retrying in 3s.",
                settings["host"],
                settings["port"],
                type(exc).__name__,
            )
            stop_event.wait(3)

    while not stop_event.is_set():
        store.heartbeat()
        stop_event.wait(2)

    if connected_once:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass
    store.set_mqtt(False, "gateway stopped")
    LOG.info("gateway stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
