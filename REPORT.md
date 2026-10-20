# EDGEWATCH project report

**Subject:** Applied Edge Analytics  
**Problem:** Interoperable IoT monitoring for mixed operating systems  
**Date of the recorded run:** 20 October 2026

## What this prototype is

EDGEWATCH shows how devices that do not share a message format can still appear on one dashboard. Three Python programs publish different JSON documents to a local Mosquitto broker. A gateway validates each document, maps it into one schema, stores it for the dashboard, and forwards a copy toward Azure IoT Hub.

The device programs are **simulations inspired by Contiki-NG and FreeRTOS**. They print that fact when they start. This project did not boot Contiki-NG, TinyOS, RIOT OS, or FreeRTOS, and it did not measure their RAM or flash use.

The operating-system table below is a **conceptual comparison from published descriptions of those systems**, not a benchmark from this prototype.

## How the edge layer is used

The gateway is the edge-processing step. It runs beside the broker, on the same computer, and does four small jobs before anything is shown or forwarded:

1. Reject malformed JSON and payloads that miss required fields.
2. Rename each device’s fields into the common schema and add a UTC timestamp.
3. Keep the latest five temperature readings and their average.
4. Record how long that local work took, then call the sender.

There is no machine-learning model and no separate analytics service. The prototype does not drop fields, so it does not claim a bandwidth saving.

## Interoperability

| Device | Native topic | Native fields | Common type | Common readings |
| --- | --- | --- | --- | --- |
| Contiki-style sim | `devices/contiki-01/telemetry` | `node_id`, `temp_c`, `battery_mv` | `temperature_sensor` | `temperature_c`, `battery_mv` |
| FreeRTOS-style sim | `devices/freertos-01/telemetry` | `deviceId`, `sensor`, `value`, `unit` | `humidity_sensor` | `humidity_percent` |
| Air-quality sim | `devices/airquality-01/telemetry` | `sensorId`, `pm25`, `pm10` | `air_quality_sensor` | `pm25_ug_m3`, `pm10_ug_m3` |

Observed native messages from the seeded MQTT run:

```json
{"node_id": "contiki-01", "temp_c": 24.5, "battery_mv": 3291}
{"deviceId": "freertos-01", "sensor": "humidity", "value": 48.7, "unit": "%"}
{"sensorId": "airquality-01", "pm25": 10.2, "pm10": 22.3}
```

The same gateway turned the first of those into:

```json
{
  "device_id": "contiki-01",
  "device_type": "temperature_sensor",
  "timestamp": "2026-10-20T16:00:22.115Z",
  "readings": {"temperature_c": 24.5, "battery_mv": 3291},
  "metadata": {"source_format": "contiki_native"}
}
```

Humidity became `humidity_percent` with `source_format` `freertos_native`. PM2.5 and PM10 became `pm25_ug_m3` and `pm10_ug_m3` with `source_format` `airquality_native`. The dashboard reads only this shape.

### What it takes to add a device

Device C was the extensibility check. Adding it required:

- a publisher, `devices/airquality_sim.py`
- one mapping branch in `gateway/normalizer.py`

It did not require a change to `dashboard/templates/index.html`, `dashboard/static/dashboard.js`, or `dashboard/static/style.css`. Those files do not contain `contiki-01`, `freertos-01`, or `airquality-01`. A pytest checks that. Cards, the table, and extra charts are built by looping over whatever numeric readings arrive.

A new native format can still need a gateway adapter. The common schema is what keeps the dashboard stable.

## Conceptual operating-system comparison

This table is literature context for the viva. It is not a result from running those kernels.

| System | Typical use | Resource constraints | Networking | Integration notes |
| --- | --- | --- | --- | --- |
| Contiki-NG | Low-power wireless sensor research and IPv6 sensor nets | Small C system, often tens of KB of RAM and a modest ROM budget | Built-in IPv6, 6LoWPAN, and RPL. CoAP is common. A full MQTT client is heavy for the smallest nodes, so a border router or gateway is normal. | Event-driven C. Sensor apps often use their own field names and a compact payload. |
| TinyOS | Early academic wireless sensor networks | Very small memory. Programs are built from nesC components. | Active Messages and collection protocols. IP support existed but was not the usual application style. | The programming model is different from ordinary C, so new devices are harder to integrate. It is less common in new products. |
| RIOT OS | General microcontroller IoT, including real-time sensing | Microkernel aimed at 32-bit MCUs, with a small RAM floor compared with Linux | Its own network stack, including 6LoWPAN. MQTT can be added, but many nodes still speak CoAP. | Standard C threads are closer to desktop programming than TinyOS, which lowers integration effort. |
| FreeRTOS | Commercial real-time control. It is a kernel, not a full operating system. | The kernel is small. Total RAM depends on tasks, heap, and whichever TCP stack is linked. | No single built-in network stack. Products add lwIP, a vendor SDK, or MQTT libraries. | Tasks and queues are familiar. Message formats are chosen by the application, which is why two FreeRTOS products may not match. |

The prototype’s lesson is the integration row: different systems encourage different payload habits. A gateway that owns the mapping lets a new device join the same dashboard.

## Measured results

Command: `python scripts/measure_local.py`  
Broker: Mosquitto already listening on `127.0.0.1:1883`  
Azure mode for this run: **mock**. The script clears the device connection string so it does not send a batch to IoT Hub.  
`azure_portal_verified` in `results/latest_run.json` is **false**. A mock count is not cloud delivery.

### MQTT run (the end-to-end local path)

Each simulator published 3 messages at a 1 second interval (`--count 3 --interval 1 --seed 1`). The script also published 2 invalid payloads.

| Measurement | Result |
| --- | --- |
| Messages published by the three simulators | 9 |
| Invalid messages published on purpose | 2 |
| Messages received by the gateway | 11 |
| Invalid messages | 2 |
| Normalized messages | 9 |
| Successful mock forwards | 9 |
| Failed mock forwards | 0 |
| Devices that appeared | `airquality-01`, `contiki-01`, `freertos-01` |
| Simulator exit codes | 0, 0, 0 |
| Compact normalized payload size | 182 to 201 bytes |
| Processing latency | average 0.050 ms, samples 0.033 to 0.098 ms |
| Forward-call latency | average 0.004 ms, samples 0.002 to 0.006 ms |
| Local pipeline (processing + forward call) | about 0.054 ms average |

Temperature window after the three Contiki-style readings: 24.5, 24.5, 24.4. Average **24.47 °C**, labeled as 3 of the latest 5 because only three temperature messages were sent.

### What the two latency numbers cover

- **Processing:** `time.perf_counter` from the start of handling one MQTT payload until the normalized message is stored, before the sender is called.
- **Forward call:** `time.perf_counter` around `sender.send` only. In this run that is an in-memory mock. It is not the time until Azure IoT Hub shows the message.
- **Local pipeline:** those two averages added. It excludes the simulator’s sleep and it is not a measured round trip to Azure.

The per-message samples are in `results/latest_run.json`.

### Separate in-process check

Calling the same Python functions without MQTT, using the example values from the problem statement, produced compact payloads of 198 bytes (temperature), 182 bytes (humidity), and 201 bytes (air quality). A separate mock sender forced to fail incremented the failed-forward count and did not increment success. That check is also in `results/latest_run.json` under `in_process`.

### Automated tests

`python -m pytest` on 20 October 2026: **15 passed**.

Covered cases: temperature, humidity, and air-quality normalization; identical top-level schema; malformed JSON; missing fields; non-numeric values; unfamiliar reading fields such as `co2_ppm`; device online/offline from timestamps; mock Azure failure; refusal of a service connection string; dashboard source not naming the three device IDs.

## Azure status for this workspace

No device connection string was available while building this project, so live IoT Hub delivery was **not** tested and must not be claimed. The code path is ready:

1. Register device `edge-gateway-01` on the existing hub.
2. Put its **device** connection string in `.env`.
3. Set `AZURE_MODE=live` and restart the gateway.
4. Confirm the body in Azure IoT Explorer or the VS Code IoT Hub monitor.

Until that confirmation, the dashboard stays in mock mode and says the sends are not cloud delivery.

## Demonstration checklist

1. Start Mosquitto, or confirm it is already listening on `127.0.0.1:1883`.
2. Start `python gateway/gateway.py` and `python dashboard/app.py`.
3. Start `python devices/contiki_sim.py` and `python devices/freertos_sim.py`.
4. In those terminals, show the two different native JSON lines.
5. In the gateway terminal, show the normalized JSON.
6. On the dashboard, show both devices, the temperature and humidity charts, and separate MQTT and Azure status.
7. For a live hub only: confirm the payload in an IoT Hub monitoring tool. Do not treat the dashboard alone as that proof.
8. Start `python devices/airquality_sim.py`.
9. Show PM2.5 and PM10 on the same page.
10. Show that no file under `dashboard/` was edited for Device C.

## Suggested team responsibilities

1. **Device simulations** — `devices/`, MQTT topics, and the native JSON examples.
2. **Gateway mapping** — `gateway/normalizer.py`, validation, and the common schema.
3. **Cloud forwarding** — Azure device identity `edge-gateway-01`, `.env`, and the difference between SDK success and portal proof.
4. **Dashboard** — layout, live states, charts, and the rule that new readings render without a UI change.
5. **Evidence** — pytest, `scripts/measure_local.py`, this report, and the operating-system comparison.

## Viva questions

**Why is this an edge project?**  
The gateway processes messages locally: it validates, renames fields, timestamps them, and averages temperature before the cloud send.

**Are Contiki and FreeRTOS running?**  
No. The programs are Python simulations with those naming styles. The OS table is conceptual.

**Why can Device C appear without a dashboard edit?**  
The page loops over `readings`. It does not keep a list of device IDs. A new native format still needs a gateway mapping.

**What is the difference between Online and the Azure card?**  
Online means a normalized reading arrived within 20 seconds. The Azure card is only the forwarding result. A device can be online while Azure is in mock mode.

**What does “SDK accepted a send” mean?**  
`send_message` returned. It does not prove the IoT Hub portal has the body. This dashboard does not read from Azure.

**Why is the broker on localhost?**  
This is a student prototype. An open unauthenticated broker should not be reachable from the internet.

**Why QoS 1?**  
The publisher asks for at least one delivery acknowledgment. The project does not build a retry queue beyond what Paho and Mosquitto already do.

**What happens to a bad payload?**  
The gateway counts it as invalid, writes an activity line, and does not forward it. The process keeps running.

**What do 0.050 ms and 0.004 ms mean?**  
They are local timer samples around normalize-and-store, and around the mock send. They are not Azure network latency.

**Why is the temperature average 24.47 °C after only three readings?**  
The window holds at most five values. The run only produced three, so the average uses those three and the UI says so.

## Limitations

- Live Azure delivery was not verified here.
- Resource numbers for Contiki-NG, TinyOS, RIOT, and FreeRTOS were not measured.
- Gateway counters reset when the gateway process restarts. State is a JSON file, not a database.
- Paho MQTT is pinned to 1.6 because `azure-iot-device` 2.14 does not accept Paho 2.
- Forward latency in the recorded run is the mock call, which is much smaller than a real TLS send to IoT Hub would be.
