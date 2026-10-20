"""Flask API and EDGEWATCH page.

The dashboard reads the gateway snapshot. It does not connect to Azure IoT Hub.

    python dashboard/app.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.presentation import decorate_state
from gateway.state_store import empty_state

load_dotenv(ROOT / ".env")


def state_file() -> Path:
    override = os.getenv("DASHBOARD_STATE_PATH")
    if override:
        return Path(override)
    data_dir = Path(os.getenv("EDGEWATCH_DATA_DIR", ROOT / "data"))
    return data_dir / "live_state.json"


def read_state_file(path: Path):
    if not path.exists():
        return None, None
    last_error = None
    for _ in range(2):
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except json.JSONDecodeError as exc:
            last_error = exc
            time.sleep(0.05)
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    return None, last_error


def build_payload() -> dict:
    raw, error = read_state_file(state_file())
    if raw is None:
        raw = empty_state()
    offline_seconds = int(os.getenv("DEVICE_OFFLINE_SECONDS", "20"))
    stale_seconds = int(os.getenv("GATEWAY_STALE_SECONDS", "12"))
    payload = decorate_state(raw, datetime.now(timezone.utc), offline_seconds, stale_seconds)
    if error is not None:
        payload["read_error"] = "The gateway state file could not be read. Restart the gateway if this continues."
    else:
        payload["read_error"] = None
    return payload


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
    )
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.json.sort_keys = False

    @app.after_request
    def no_cache(response):
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/state")
    def state():
        return jsonify(build_payload())

    return app


app = create_app()


def main() -> int:
    port = int(os.getenv("FLASK_PORT", "5000"))
    print(f"EDGEWATCH dashboard at http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
