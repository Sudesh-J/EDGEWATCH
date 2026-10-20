"""Gateway handling, unfamiliar dashboard fields, and mock Azure failure."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pathlib import Path

from dashboard.app import create_app
from dashboard.presentation import decorate_state, format_reading_fields
from gateway.azure_sender import MockAzureSender, create_sender
from gateway.gateway import handle_raw_message
from gateway.normalizer import utc_now_iso
from gateway.state_store import StateStore, empty_state


def _publish(store, sender, topic, payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    handle_raw_message(store, sender, topic, raw)


def test_malformed_json_is_counted_and_does_not_forward():
    store = StateStore(path=None)
    sender = MockAzureSender()
    _publish(store, sender, "devices/bad-01/telemetry", "{not-json")
    state = store.snapshot()
    assert state["counters"]["received"] == 1
    assert state["counters"]["invalid"] == 1
    assert state["counters"]["azure_success"] == 0
    assert sender.sent == []
    assert state["activity"][0]["type"] == "invalid_payload"


def test_azure_mock_failure_does_not_count_as_success():
    store = StateStore(path=None)
    sender = MockAzureSender(fail=True)
    _publish(
        store,
        sender,
        "devices/contiki-01/telemetry",
        {"node_id": "contiki-01", "temp_c": 25.0, "battery_mv": 3100},
    )
    state = store.snapshot()
    assert state["counters"]["normalized"] == 1
    assert state["counters"]["azure_success"] == 0
    assert state["counters"]["azure_failed"] == 1
    assert state["azure"]["last_error"] == "mock failure requested"
    assert any(item["type"] == "azure_failed" for item in state["activity"])


def test_temperature_rolling_average_uses_latest_five():
    store = StateStore(path=None)
    sender = MockAzureSender()
    for temperature in (20, 22, 24, 26, 28, 30):
        _publish(
            store,
            sender,
            "devices/contiki-01/telemetry",
            {"node_id": "contiki-01", "temp_c": temperature, "battery_mv": 3000},
        )
    analytics = store.snapshot()["analytics"]
    assert analytics["temperature_window"] == [22, 24, 26, 28, 30]
    assert analytics["temperature_avg_c"] == 26.0


def test_dashboard_keeps_unfamiliar_reading_fields():
    rows = format_reading_fields({"co2_ppm": 415, "noise_level": 40, "pm25_ug_m3": 12.5})
    assert [row["key"] for row in rows] == ["co2_ppm", "noise_level", "pm25_ug_m3"]
    assert rows[0]["label"] == "CO2"
    assert rows[0]["unit"] == "ppm"
    assert rows[1]["label"] == "Noise Level"
    assert rows[1]["unit"] == ""
    assert rows[2]["label"] == "PM25"

    store = StateStore(path=None)
    _publish(
        store,
        MockAzureSender(),
        "devices/custom-01/telemetry",
        {"sensorId": "custom-01", "pm25": 9, "pm10": 14},
    )
    raw = store.snapshot()
    raw["devices"]["custom-01"]["readings"]["co2_ppm"] = 415
    now = datetime.now(timezone.utc)
    decorated = decorate_state(raw, now, offline_seconds=20, stale_seconds=30)
    readings = decorated["devices"]["custom-01"]["readings"]
    assert readings["co2_ppm"] == 415
    assert readings["pm25_ug_m3"] == 9
    shown = [item["key"] for item in decorated["devices"]["custom-01"]["display_readings"]]
    assert "co2_ppm" in shown
    assert "pm10_ug_m3" in shown


def test_device_status_uses_message_time_not_azure():
    raw = empty_state()
    now = datetime.now(timezone.utc)
    raw["updated_at"] = utc_now_iso()
    raw["mqtt"]["connected"] = True
    raw["devices"]["contiki-01"] = {
        "device_id": "contiki-01",
        "device_type": "temperature_sensor",
        "readings": {"temperature_c": 24.0},
        "last_seen": (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "history": [],
    }
    raw["devices"]["freertos-01"] = {
        "device_id": "freertos-01",
        "device_type": "humidity_sensor",
        "readings": {"humidity_percent": 50},
        "last_seen": (now - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "history": [],
    }
    decorated = decorate_state(raw, now, offline_seconds=20, stale_seconds=12)
    assert decorated["devices"]["contiki-01"]["status"] == "online"
    assert decorated["devices"]["freertos-01"]["status"] == "offline"
    assert decorated["summary"]["recently_active"] == 1
    assert decorated["azure"]["mode"] == "unknown"


def test_api_returns_gateway_waiting_state(monkeypatch, tmp_path):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("DASHBOARD_STATE_PATH", str(missing))
    client = create_app().test_client()
    response = client.get("/api/state")
    assert response.status_code == 200
    body = response.get_json()
    assert body["summary"]["discovered_devices"] == 0
    assert body["gateway"]["online"] is False
    assert "Waiting" in body["mqtt"]["status_message"] or "not reporting" in body["mqtt"]["status_message"]


def test_dashboard_source_is_not_tied_to_device_ids():
    js = Path("dashboard/static/dashboard.js").read_text(encoding="utf-8")
    html = Path("dashboard/templates/index.html").read_text(encoding="utf-8")
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    for needle in ("contiki-01", "freertos-01", "airquality-01", "contiki_native", "freertos_native"):
        assert needle not in js
        assert needle not in html
        assert needle not in css


def test_auto_mode_without_credentials_uses_mock(monkeypatch):
    monkeypatch.setenv("AZURE_MODE", "auto")
    monkeypatch.setenv("IOTHUB_DEVICE_CONNECTION_STRING", "")
    sender, info = create_sender()
    assert sender.mode == "mock"
    assert info["delivery"] == "local_mock"
    assert sender.send({"device_id": "contiki-01"}).ok is True


def test_service_connection_string_fails_without_sending(monkeypatch):
    monkeypatch.setenv("AZURE_MODE", "live")
    monkeypatch.setenv(
        "IOTHUB_DEVICE_CONNECTION_STRING",
        "HostName=hub.azure-devices.net;SharedAccessKeyName=iothubowner;SharedAccessKey=abc",
    )
    sender, info = create_sender()
    result = sender.send({"device_id": "contiki-01"})
    assert result.ok is False
    assert "service" in (result.error or "").lower()
    assert info["delivery"] == "blocked"
    assert "SharedAccessKey=abc" not in (result.error or "")
