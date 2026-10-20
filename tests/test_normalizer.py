"""Normalization, invalid payloads, and the common schema."""

from gateway.normalizer import COMMON_KEYS, NormalizationError, normalize_message
import pytest

TIMESTAMP = "2026-01-01T00:00:00.000Z"


def test_temperature_message_normalization():
    native = {"node_id": "contiki-01", "temp_c": 28.4, "battery_mv": 2950, "seq": 4}
    result = normalize_message(native, timestamp=TIMESTAMP)
    assert result == {
        "device_id": "contiki-01",
        "device_type": "temperature_sensor",
        "timestamp": TIMESTAMP,
        "readings": {"temperature_c": 28.4, "battery_mv": 2950},
        "metadata": {"source_format": "contiki_native"},
    }


def test_humidity_message_normalization():
    native = {"deviceId": "freertos-01", "sensor": "humidity", "value": 64.0, "unit": "%"}
    result = normalize_message(native, timestamp=TIMESTAMP)
    assert result["device_id"] == "freertos-01"
    assert result["device_type"] == "humidity_sensor"
    assert result["readings"] == {"humidity_percent": 64.0}
    assert result["metadata"] == {"source_format": "freertos_native"}
    assert "unit" not in result
    assert "sensor" not in result["readings"]


def test_air_quality_message_normalization():
    native = {"sensorId": "airquality-01", "pm25": 12.5, "pm10": 22.0}
    result = normalize_message(native, timestamp=TIMESTAMP)
    assert result["device_id"] == "airquality-01"
    assert result["device_type"] == "air_quality_sensor"
    assert result["readings"] == {"pm25_ug_m3": 12.5, "pm10_ug_m3": 22.0}
    assert result["metadata"] == {"source_format": "airquality_native"}


def test_common_schema_consistency():
    samples = [
        {"node_id": "contiki-01", "temp_c": 21, "battery_mv": 3000},
        {"deviceId": "freertos-01", "sensor": "humidity", "value": 40, "unit": "%"},
        {"sensorId": "airquality-01", "pm25": 5, "pm10": 9},
    ]
    shapes = []
    for sample in samples:
        result = normalize_message(sample, timestamp=TIMESTAMP)
        shapes.append((tuple(result.keys()), tuple(result["metadata"].keys())))
        assert set(result) == set(COMMON_KEYS)
        assert isinstance(result["readings"], dict) and result["readings"]
    assert len(set(shapes)) == 1


def test_missing_required_fields():
    with pytest.raises(NormalizationError, match="missing field: battery_mv"):
        normalize_message({"node_id": "contiki-01", "temp_c": 22.0})
    with pytest.raises(NormalizationError, match="missing field: value"):
        normalize_message({"deviceId": "freertos-01", "sensor": "humidity", "unit": "%"})
    with pytest.raises(NormalizationError, match="missing field: pm10"):
        normalize_message({"sensorId": "airquality-01", "pm25": 10})


def test_rejects_non_numeric_and_unknown_shapes():
    with pytest.raises(NormalizationError, match="must be a number"):
        normalize_message({"node_id": "contiki-01", "temp_c": "hot", "battery_mv": 3000})
    with pytest.raises(NormalizationError, match="must be a number"):
        normalize_message({"node_id": "contiki-01", "temp_c": True, "battery_mv": 3000})
    with pytest.raises(NormalizationError, match="unrecognized"):
        normalize_message({"foo": 1})
    with pytest.raises(NormalizationError, match="JSON object"):
        normalize_message(["not", "an", "object"])
