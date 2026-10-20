"""Contiki-NG-style temperature sensor simulation.

This is a Python process inspired by a Contiki-NG sensor node. It does not run
the Contiki-NG kernel. It publishes a native JSON payload that is different
from the FreeRTOS-style humidity simulator.

    python devices/contiki_sim.py
    python devices/contiki_sim.py --interval 2 --count 5
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    pass

DEVICE_ID = "contiki-01"
TOPIC = f"devices/{DEVICE_ID}/telemetry"
DEFAULT_INTERVAL = 5.0
stop = False


def request_stop(signum, frame):
    global stop
    stop = True


def parse_args():
    parser = argparse.ArgumentParser(description="Simulated Contiki-NG-style temperature sensor")
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="Seconds between messages")
    parser.add_argument("--count", type=int, default=0, help="Stop after this many messages. 0 runs until Ctrl+C.")
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args()


def wait_seconds(seconds: float) -> bool:
    """Sleep in short steps. Return True if a stop was requested."""
    remaining = seconds
    while remaining > 0 and not stop:
        step = min(0.2, remaining)
        time.sleep(step)
        remaining -= step
    return stop


def main() -> int:
    global stop
    args = parse_args()
    if args.interval <= 0:
        print("[contiki-01] interval must be greater than 0", file=sys.stderr)
        return 2
    if args.seed is not None:
        random.seed(args.seed)

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)

    print("[contiki-01] Contiki-NG-style Python simulation. The Contiki-NG OS is not running.")
    print(f"[contiki-01] publishing to {TOPIC} every {args.interval:.1f}s")

    connected = {"ok": False}
    client = mqtt.Client(client_id="contiki-01-sim")
    client.reconnect_delay_set(min_delay=1, max_delay=10)

    def on_connect(mqtt_client, userdata, flags, rc):
        connected["ok"] = rc == 0
        if connected["ok"]:
            print(f"[contiki-01] connected to {args.host}:{args.port}", flush=True)
        else:
            print(f"[contiki-01] MQTT connect failed: rc={rc}", flush=True)

    def on_disconnect(mqtt_client, userdata, rc):
        connected["ok"] = False
        if not stop:
            print(f"[contiki-01] MQTT disconnected (rc={rc}). Waiting to retry.", flush=True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    while not stop:
        try:
            client.connect(args.host, args.port, keepalive=30)
            client.loop_start()
            break
        except Exception as exc:
            print(f"[contiki-01] broker unavailable ({type(exc).__name__}). Retrying in 3s.", flush=True)
            if wait_seconds(3):
                break

    temperature = random.uniform(24.0, 30.0)
    battery = random.randint(3100, 3300)
    sent = 0
    announced_wait = False
    try:
        while not stop and (args.count == 0 or sent < args.count):
            if not client.is_connected():
                if not announced_wait:
                    if wait_seconds(0.6):
                        break
                    if client.is_connected():
                        continue
                    print("[contiki-01] broker unavailable, waiting", flush=True)
                    announced_wait = True
                elif wait_seconds(1):
                    break
                continue
            announced_wait = False
            temperature = min(35.0, max(20.0, temperature + random.uniform(-0.4, 0.4)))
            battery = min(3400, max(2800, battery + random.randint(-4, 1)))
            payload = {
                "node_id": DEVICE_ID,
                "temp_c": round(temperature, 1),
                "battery_mv": int(battery),
            }
            try:
                info = client.publish(TOPIC, json.dumps(payload), qos=1)
                info.wait_for_publish(timeout=3)
                if not info.is_published():
                    print("[contiki-01] publish was not acknowledged", flush=True)
                    wait_seconds(2)
                    continue
            except Exception as exc:
                print(f"[contiki-01] publish failed ({type(exc).__name__}). Will retry.", flush=True)
                wait_seconds(2)
                continue
            sent += 1
            print(f"[contiki-01] published {json.dumps(payload)}", flush=True)
            if args.count and sent >= args.count:
                break
            wait_seconds(args.interval)
    finally:
        stop = True
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
    print("[contiki-01] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
