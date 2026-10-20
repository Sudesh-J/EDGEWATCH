"""Turn gateway state into the JSON the dashboard displays.

Reading labels are derived from field names and suffixes. There is no
per-device whitelist, so a new normalized reading still appears.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from gateway.state_store import empty_state

SUFFIXES = (
    ("_ug_m3", "µg/m³"),
    ("_percent", "%"),
    ("_mv", "mV"),
    ("_ppm", "ppm"),
    ("_c", "°C"),
)


def parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_reading_fields(readings: dict) -> list[dict]:
    """Describe every numeric or scalar reading without dropping unknown keys."""
    rows = []
    if not isinstance(readings, dict):
        return rows
    for key, value in readings.items():
        unit = ""
        base = str(key)
        for suffix, suffix_unit in SUFFIXES:
            if base.endswith(suffix):
                unit = suffix_unit
                base = base[: -len(suffix)]
                break
        pm_digits = ""
        if base.startswith("pm") and len(base) > 2 and base[2:].isdigit():
            pm_digits = base[2:]
        if pm_digits:
            label = f"PM{pm_digits}"
        elif base.isalnum() and len(base) <= 4:
            label = base.upper()
        else:
            label = base.replace("_", " ").strip().title() or str(key)
        rows.append({"key": key, "label": label, "value": value, "unit": unit})
    return rows


def decorate_state(raw: dict | None, now: datetime, offline_seconds: int, stale_seconds: int) -> dict:
    """Add online/offline status at read time so it stays fresh between writes."""
    state = deepcopy(raw) if raw else empty_state()
    updated = parse_iso(state.get("updated_at"))
    if updated is None:
        age = None
        gateway_online = False
    else:
        age = max(0.0, (now - updated).total_seconds())
        gateway_online = age <= stale_seconds

    state.setdefault("gateway", {})
    state["gateway"]["online"] = gateway_online
    state["gateway"]["heartbeat_age_seconds"] = None if age is None else round(age, 1)
    state.setdefault("mqtt", {})
    state["mqtt"]["trusted"] = gateway_online
    if not gateway_online:
        if state.get("updated_at"):
            state["mqtt"]["status_message"] = "Gateway is not reporting. Start python gateway/gateway.py."
        else:
            state["mqtt"]["status_message"] = "Waiting for the gateway."
    elif state["mqtt"].get("connected"):
        state["mqtt"]["status_message"] = "MQTT broker connected."
    else:
        state["mqtt"]["status_message"] = state["mqtt"].get("error") or "MQTT broker unavailable."

    active = 0
    devices = state.get("devices") or {}
    for device in devices.values():
        seen = parse_iso(device.get("last_seen"))
        if seen is not None and (now - seen).total_seconds() <= offline_seconds:
            device["status"] = "online"
            active += 1
        else:
            device["status"] = "offline"
        device["display_readings"] = format_reading_fields(device.get("readings") or {})
    state["summary"] = {
        "discovered_devices": len(devices),
        "recently_active": active,
        "offline_after_seconds": offline_seconds,
    }
    return state
