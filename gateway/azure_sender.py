"""Forward normalized telemetry with the Azure IoT device SDK, or a local mock.

Mock sends are stored only in this process. They are not cloud delivery.
A successful live SDK call still has to be confirmed in Azure IoT Hub tools.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass

DEVICE_IDENTITY = "edge-gateway-01"
_PLACEHOLDER_TOKENS = ("your-hub", "your_hub", "paste", "changeme", "example", "<", "xxxx")


@dataclass
class SendResult:
    ok: bool
    mode: str
    error: str | None = None


def redact(text: str) -> str:
    cleaned = re.sub(r"SharedAccessKey=[^;\s'\"]+", "SharedAccessKey=***", text)
    if len(cleaned) > 300:
        return cleaned[:300]
    return cleaned


def connection_parts(connection_string: str) -> dict:
    parts = {}
    for item in connection_string.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key] = value
    return parts


def looks_like_device_connection_string(connection_string: str) -> bool:
    if not connection_string or "SharedAccessKeyName=" in connection_string:
        return False
    required = ("HostName=", "DeviceId=", "SharedAccessKey=")
    if not all(part in connection_string for part in required):
        return False
    lowered = connection_string.lower()
    if any(token in lowered for token in _PLACEHOLDER_TOKENS):
        return False
    return True


class MockAzureSender:
    """Local stand-in used when Azure credentials are not configured."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent = []
        self.mode = "mock"

    def send(self, payload: dict) -> SendResult:
        if self.fail:
            return SendResult(False, "mock", "mock failure requested")
        self.sent.append(payload)
        return SendResult(True, "mock", None)


class FailingSender:
    """Keeps the gateway running when the credential itself is unusable."""

    def __init__(self, reason: str):
        self.mode = "live"
        self._reason = reason

    def send(self, payload: dict) -> SendResult:
        return SendResult(False, "live", self._reason)


class LiveAzureSender:
    """Send normalized JSON with IoTHubDeviceClient for edge-gateway-01."""

    def __init__(self, connection_string: str):
        if "SharedAccessKeyName=" in connection_string:
            raise ValueError(
                "Refusing a service connection string. Use the device connection string for edge-gateway-01."
            )
        if not looks_like_device_connection_string(connection_string):
            raise ValueError("Device connection string is missing HostName, DeviceId, or SharedAccessKey.")
        self._connection_string = connection_string
        self._client = None
        self._lock = threading.Lock()
        self.mode = "live"
        parts = connection_parts(connection_string)
        self.device_id = parts.get("DeviceId")
        self.hostname = parts.get("HostName")

    def _ensure_client(self):
        if self._client is not None:
            return
        from azure.iot.device import IoTHubDeviceClient

        client = IoTHubDeviceClient.create_from_connection_string(self._connection_string)
        client.connect()
        self._client = client

    def send(self, payload: dict) -> SendResult:
        try:
            from azure.iot.device import Message

            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            message = Message(body)
            message.content_type = "application/json"
            message.content_encoding = "utf-8"
            with self._lock:
                self._ensure_client()
                self._client.send_message(message)
            return SendResult(True, "live", None)
        except Exception as exc:
            self._client = None
            return SendResult(False, "live", redact(f"{type(exc).__name__}: {exc}"))


def create_sender():
    """Pick a sender from AZURE_MODE and IOTHUB_DEVICE_CONNECTION_STRING.

    Returns (sender, info). info is safe to log and to store in dashboard state.
    """
    mode = os.getenv("AZURE_MODE", "auto").strip().lower() or "auto"
    raw = os.getenv("IOTHUB_DEVICE_CONNECTION_STRING", "").strip()
    usable = looks_like_device_connection_string(raw)

    if mode not in {"auto", "mock", "live"}:
        mode = "auto"

    if mode == "mock" or (mode == "auto" and not usable):
        note = "Mock sends are counted locally and are not delivered to Azure IoT Hub."
        if mode == "auto" and raw and "SharedAccessKeyName=" in raw:
            note = "Service connection string was ignored. Mock mode is active. Use the device connection string."
        elif mode == "auto" and raw and not usable:
            note = "Connection string looks like a placeholder. Mock mode is active."
        return MockAzureSender(), {
            "mode": "mock",
            "delivery": "local_mock",
            "configured_device_id": None,
            "note": note,
            "warning": None,
        }

    if "SharedAccessKeyName=" in raw:
        reason = "Refusing a service connection string. Use the device connection string for edge-gateway-01."
        return FailingSender(reason), {
            "mode": "live",
            "delivery": "blocked",
            "configured_device_id": None,
            "note": reason,
            "warning": reason,
        }

    if not usable:
        reason = "AZURE_MODE=live but IOTHUB_DEVICE_CONNECTION_STRING is missing or still a placeholder."
        return FailingSender(reason), {
            "mode": "live",
            "delivery": "blocked",
            "configured_device_id": None,
            "note": reason,
            "warning": reason,
        }

    sender = LiveAzureSender(raw)
    warning = None
    if sender.device_id != DEVICE_IDENTITY:
        warning = f"DeviceId is {sender.device_id}; this project expects {DEVICE_IDENTITY}."
    return sender, {
        "mode": "live",
        "delivery": "iot_hub_sdk",
        "configured_device_id": sender.device_id,
        "note": (
            "A successful SDK send is not proof that IoT Hub stored the message. "
            "Confirm telemetry in Azure IoT Hub monitoring tools. This dashboard does not read from Azure."
        ),
        "warning": warning,
    }
