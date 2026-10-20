"""Convert native device payloads into the EDGEWATCH common schema.

The dashboard reads only this schema. A new device can use different field
names, but it needs a small mapping here before those readings are stored.
"""

from __future__ import annotations

from datetime import datetime, timezone


class NormalizationError(ValueError):
    """Raised when a payload cannot be mapped into the common schema."""


COMMON_KEYS = ("device_id", "device_type", "timestamp", "readings", "metadata")


def utc_now_iso() -> str:
    """UTC timestamp with millisecond precision and a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _require_number(payload: dict, field: str):
    if field not in payload:
        raise NormalizationError(f"missing field: {field}")
    value = payload[field]
    # bool is a subclass of int, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NormalizationError(f"field {field} must be a number")
    return value


def _require_text(payload: dict, field: str) -> str:
    if field not in payload:
        raise NormalizationError(f"missing field: {field}")
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"field {field} must be a non-empty string")
    return value.strip()


def _envelope(device_id: str, device_type: str, readings: dict, source_format: str, timestamp: str | None) -> dict:
    return {
        "device_id": device_id,
        "device_type": device_type,
        "timestamp": timestamp or utc_now_iso(),
        "readings": readings,
        "metadata": {"source_format": source_format},
    }


def normalize_message(payload, timestamp: str | None = None) -> dict:
    """Map one native JSON object to the common telemetry schema.

    Identification is by payload shape, in this order:
    Contiki-style keys, then FreeRTOS-style keys, then air-quality keys.
    Extra native fields are dropped so the dashboard never sees them.
    """
    if not isinstance(payload, dict):
        raise NormalizationError("payload must be a JSON object")

    if any(key in payload for key in ("node_id", "temp_c", "battery_mv")):
        device_id = _require_text(payload, "node_id")
        temperature = _require_number(payload, "temp_c")
        battery = _require_number(payload, "battery_mv")
        return _envelope(
            device_id,
            "temperature_sensor",
            {"temperature_c": temperature, "battery_mv": battery},
            "contiki_native",
            timestamp,
        )

    if any(key in payload for key in ("deviceId", "sensor", "unit")):
        device_id = _require_text(payload, "deviceId")
        sensor = _require_text(payload, "sensor")
        if sensor != "humidity":
            raise NormalizationError(f"unsupported freertos sensor: {sensor}")
        value = _require_number(payload, "value")
        unit = _require_text(payload, "unit")
        if unit != "%":
            raise NormalizationError(f"unsupported humidity unit: {unit}")
        return _envelope(
            device_id,
            "humidity_sensor",
            {"humidity_percent": value},
            "freertos_native",
            timestamp,
        )

    if any(key in payload for key in ("sensorId", "pm25", "pm10")):
        device_id = _require_text(payload, "sensorId")
        pm25 = _require_number(payload, "pm25")
        pm10 = _require_number(payload, "pm10")
        return _envelope(
            device_id,
            "air_quality_sensor",
            {"pm25_ug_m3": pm25, "pm10_ug_m3": pm10},
            "airquality_native",
            timestamp,
        )

    raise NormalizationError("unrecognized payload format")
