"""
Flask dashboard. Talks to the daemon over the Unix socket only -- never
touches iptables/wg-quick/systemctl directly. Bound to 127.0.0.1 by
default; reach it via SSH tunnel, don't expose on the LAN (no auth here).
"""

import json
import socket
import sys
import os

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config_loader import load_config

SOCKET_PATH = "/tmp/nyx.sock"

app = Flask(__name__)
cfg = load_config("config.yaml")


def send_request(req: dict) -> dict:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
        return {"ok": False, "error": "daemon not running (start main.py first)"}

    client.sendall(json.dumps(req).encode())
    resp = client.recv(65536)
    client.close()
    return json.loads(resp.decode())


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Single source of truth for the dashboard: mode, tunnel health, and
    the latest IP/geolocation snapshot (cached server-side by the monitor
    loop, not re-fetched from an external API on every dashboard refresh)."""
    return jsonify(send_request({"action": "status"}))


@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    mode = request.json.get("mode")
    if mode not in ["vpn", "tor"]:
        return jsonify({"ok": False, "error": "invalid mode (only vpn/tor -- proxy is always-on)"}), 400
    return jsonify(send_request({"action": "set_mode", "mode": mode}))


@app.route("/api/rotate", methods=["POST"])
def api_rotate():
    return jsonify(send_request({"action": "rotate_vpn"}))


@app.route("/api/emergency_stop", methods=["POST"])
def api_emergency_stop():
    return jsonify(send_request({"action": "emergency_stop"}))


@app.route("/api/traffic_log")
def api_traffic_log():
    path = cfg["logging"]["traffic_log"]
    if not os.path.exists(path):
        return jsonify([])
    with open(path) as f:
        lines = f.readlines()[-100:]
    records = [json.loads(line) for line in lines if line.strip()]
    return jsonify(records)


@app.route("/api/logs")
def api_logs():
    """Tail of system.log for the dashboard's live logs panel. Simple
    poll-based tail (no streaming) -- fine at a few-second refresh
    interval for a hobby-project dashboard."""
    path = cfg["logging"]["system_log"]
    if not os.path.exists(path):
        return jsonify({"lines": []})
    with open(path) as f:
        lines = f.readlines()[-200:]
    return jsonify({"lines": [line.rstrip("\n") for line in lines]})


if __name__ == "__main__":
    dash_cfg = cfg["dashboard"]
    app.run(host=dash_cfg["host"], port=dash_cfg["port"], debug=False)
