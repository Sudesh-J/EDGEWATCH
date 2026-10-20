"""In-memory gateway state, with a JSON snapshot the dashboard can read."""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path

from gateway.normalizer import utc_now_iso


def empty_state() -> dict:
    return {
        "updated_at": None,
        "started_at": None,
        "mqtt": {
            "connected": False,
            "broker": None,
            "port": None,
            "error": None,
            "last_message_at": None,
            "last_connected_at": None,
        },
        "azure": {
            "mode": "unknown",
            "delivery": "not_configured",
            "configured_device_id": None,
            "last_success_at": None,
            "last_error": None,
            "last_error_at": None,
            "note": "Gateway has not reported Azure status.",
            "warning": None,
        },
        "gateway": {"online": False},
        "counters": {
            "received": 0,
            "invalid": 0,
            "normalized": 0,
            "azure_success": 0,
            "azure_failed": 0,
        },
        "latency": {
            "last_processing_ms": None,
            "avg_processing_ms": None,
            "last_forward_ms": None,
            "avg_forward_ms": None,
            "sample_count": 0,
            "last_payload_bytes": None,
            "min_payload_bytes": None,
            "max_payload_bytes": None,
            "processing_samples_ms": [],
            "forward_samples_ms": [],
            "processing_definition": (
                "Milliseconds from the start of handling one MQTT payload until the "
                "normalized message is stored, before the Azure send call."
            ),
            "forward_definition": (
                "Milliseconds spent inside the sender call. In mock mode this is local only. "
                "In live mode this is how long IoTHubDeviceClient.send_message blocked. "
                "It is not Azure Portal ingestion time."
            ),
        },
        "analytics": {
            "temperature_window": [],
            "temperature_avg_c": None,
            "temperature_window_size": 5,
        },
        "devices": {},
        "activity": [],
    }


class StateStore:
    """Thread-safe state for the MQTT callback and the heartbeat loop."""

    def __init__(self, path: Path | None, log_path: Path | None = None):
        self.path = path
        self.log_path = log_path
        self._lock = threading.Lock()
        self._state = empty_state()
        self._processing_samples: list[float] = []
        self._forward_samples: list[float] = []
        self._payload_sizes: list[int] = []

    def configure(self, broker: str, port: int, azure_info: dict) -> None:
        with self._lock:
            now = utc_now_iso()
            self._state["started_at"] = now
            self._state["updated_at"] = now
            self._state["mqtt"]["broker"] = broker
            self._state["mqtt"]["port"] = port
            self._state["mqtt"]["error"] = "connecting"
            self._state["azure"].update(
                {
                    "mode": azure_info.get("mode", "unknown"),
                    "delivery": azure_info.get("delivery", "not_configured"),
                    "configured_device_id": azure_info.get("configured_device_id"),
                    "note": azure_info.get("note") or "",
                    "warning": azure_info.get("warning"),
                }
            )
            self._save_locked()

    def set_mqtt(self, connected: bool, error: str | None) -> None:
        with self._lock:
            now = utc_now_iso()
            self._state["updated_at"] = now
            self._state["mqtt"]["connected"] = connected
            self._state["mqtt"]["error"] = error
            if connected:
                self._state["mqtt"]["last_connected_at"] = now
            self._save_locked()

    def heartbeat(self) -> None:
        with self._lock:
            self._state["updated_at"] = utc_now_iso()
            self._save_locked()

    def record_invalid(self, topic: str, reason: str) -> None:
        with self._lock:
            now = utc_now_iso()
            self._state["updated_at"] = now
            self._state["counters"]["received"] += 1
            self._state["counters"]["invalid"] += 1
            self._state["mqtt"]["last_message_at"] = now
            self._activity("invalid_payload", f"{topic}: {reason}")
            self._append_log({"kind": "invalid", "topic": topic, "reason": reason, "timestamp": now})
            self._save_locked()

    def record_normalized(self, message: dict, processing_ms: float, topic: str) -> None:
        with self._lock:
            now = message["timestamp"]
            self._state["updated_at"] = utc_now_iso()
            self._state["counters"]["received"] += 1
            self._state["counters"]["normalized"] += 1
            self._state["mqtt"]["last_message_at"] = now
            self._remember_processing(processing_ms)
            device_id = message["device_id"]
            devices = self._state["devices"]
            current = devices.get(device_id, {"message_count": 0, "history": []})
            history = list(current.get("history", []))
            history.append({"timestamp": now, "readings": dict(message["readings"])})
            del history[:-36]
            devices[device_id] = {
                "device_id": device_id,
                "device_type": message["device_type"],
                "readings": dict(message["readings"]),
                "metadata": dict(message["metadata"]),
                "last_seen": now,
                "message_count": int(current.get("message_count", 0)) + 1,
                "history": history,
                "last_topic": topic,
            }
            readings = message["readings"]
            if "temperature_c" in readings:
                window = self._state["analytics"]["temperature_window"]
                window.append(readings["temperature_c"])
                del window[:-5]
                self._state["analytics"]["temperature_avg_c"] = round(sum(window) / len(window), 2)
            self._activity("mqtt_received", f"{topic} from {device_id}")
            self._activity(
                "normalized",
                f"{device_id} {message['device_type']} via {message['metadata']['source_format']}",
            )
            self._append_log(
                {
                    "kind": "normalized",
                    "topic": topic,
                    "processing_ms": round(processing_ms, 3),
                    "message": message,
                    "timestamp": now,
                }
            )
            self._save_locked()

    def record_forward(self, device_id: str, result, forward_ms: float, payload_bytes: int) -> None:
        with self._lock:
            now = utc_now_iso()
            self._state["updated_at"] = now
            self._remember_forward(forward_ms, payload_bytes)
            if result.ok:
                self._state["counters"]["azure_success"] += 1
                self._state["azure"]["last_success_at"] = now
                self._state["azure"]["last_error"] = None
                label = "mock, not cloud delivery" if result.mode == "mock" else "SDK send returned"
                self._activity("azure_success", f"{device_id}: {label}")
            else:
                self._state["counters"]["azure_failed"] += 1
                self._state["azure"]["last_error"] = result.error or "send failed"
                self._state["azure"]["last_error_at"] = now
                self._activity("azure_failed", f"{device_id}: {result.error or 'send failed'}")
            self._append_log(
                {
                    "kind": "forward",
                    "device_id": device_id,
                    "ok": result.ok,
                    "mode": result.mode,
                    "error": result.error,
                    "forward_ms": round(forward_ms, 3),
                    "payload_bytes": payload_bytes,
                    "timestamp": now,
                }
            )
            self._save_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._state)

    def _remember_processing(self, processing_ms: float) -> None:
        self._processing_samples.append(processing_ms)
        del self._processing_samples[:-50]
        latency = self._state["latency"]
        latency["last_processing_ms"] = round(processing_ms, 3)
        latency["avg_processing_ms"] = round(sum(self._processing_samples) / len(self._processing_samples), 3)
        latency["sample_count"] = len(self._processing_samples)
        latency["processing_samples_ms"] = [round(value, 3) for value in self._processing_samples]

    def _remember_forward(self, forward_ms: float, payload_bytes: int) -> None:
        self._forward_samples.append(forward_ms)
        self._payload_sizes.append(payload_bytes)
        del self._forward_samples[:-50]
        del self._payload_sizes[:-50]
        latency = self._state["latency"]
        latency["last_forward_ms"] = round(forward_ms, 3)
        latency["avg_forward_ms"] = round(sum(self._forward_samples) / len(self._forward_samples), 3)
        latency["last_payload_bytes"] = payload_bytes
        latency["min_payload_bytes"] = min(self._payload_sizes)
        latency["max_payload_bytes"] = max(self._payload_sizes)
        latency["forward_samples_ms"] = [round(value, 3) for value in self._forward_samples]

    def _activity(self, event_type: str, detail: str) -> None:
        self._state["activity"].insert(
            0,
            {"timestamp": utc_now_iso(), "type": event_type, "detail": detail},
        )
        del self._state["activity"][40:]

    def _append_log(self, event: dict) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _save_locked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            return
