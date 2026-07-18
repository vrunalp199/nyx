"""
Entry point / daemon. Owns:
  - the single Controller (mode: vpn|tor)
  - proxy lifecycle -- started ONCE here, unconditionally, and never
    stopped as part of mode switching (see README "proxy is not a mode")
  - VPN rotation scheduler thread
  - kill-switch watchdog thread
  - monitor loop (IP/bandwidth polling, feeds the dashboard via
    controller.update_snapshot())
  - Unix socket IPC for CLI/dashboard

Run with: sudo python3 main.py
"""

import json
import os
import socket
import threading
import time
import sys

from core.config_loader import load_config
from core.controller import Controller, ModeSwitchError
from core.scheduler import VPNRotationScheduler
from core.watchdog import KillSwitchWatchdog
from logger.logger import get_system_logger, TrafficLogger
from monitor.ip_monitor import get_ip_info
from monitor.bandwidth import get_bandwidth
from monitor.resource_monitor import check_resources
from network import proxy_manager, firewall

SOCKET_PATH = "/tmp/nyx.sock"


def monitor_loop(cfg, controller, traffic_logger, stop_event):
    interval = cfg["monitor"].get("poll_interval_seconds", 5)
    while not stop_event.is_set():
        ip_info = get_ip_info(cfg)
        bw = get_bandwidth()
        controller.update_snapshot(ip_info, bw)
        traffic_logger.log(ip_info, bw, controller.current_mode)

        resource_snapshot = check_resources(cfg)
        controller.update_resource_snapshot(resource_snapshot)

        stop_event.wait(interval)


def handle_client(conn, controller):
    try:
        data = conn.recv(4096)
        if not data:
            return
        req = json.loads(data.decode())
        action = req.get("action")

        if action == "set_mode":
            try:
                result = controller.set_mode(req["mode"])
                response = {"ok": True, "result": result}
            except (ModeSwitchError, ValueError) as e:
                response = {"ok": False, "error": str(e)}
        elif action == "rotate_vpn":
            try:
                result = controller.rotate_vpn()
                response = {"ok": True, "result": result}
            except ModeSwitchError as e:
                response = {"ok": False, "error": str(e)}
        elif action == "emergency_stop":
            response = {"ok": True, "result": controller.emergency_stop()}
        elif action == "status":
            response = {"ok": True, "result": controller.status()}
        else:
            response = {"ok": False, "error": f"Unknown action: {action}"}

        conn.sendall(json.dumps(response).encode())
    except Exception as e:
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode())
        except OSError:
            pass
    finally:
        conn.close()


def ipc_server(controller, stop_event):
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    server.listen(5)
    server.settimeout(1.0)

    while not stop_event.is_set():
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        handle_client(conn, controller)

    server.close()
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)


def main():
    if os.geteuid() != 0:
        print("This must be run as root (sudo python3 main.py) -- it needs "
              "to manage iptables, wg-quick, and systemd services.")
        sys.exit(1)

    cfg = load_config("config.yaml")
    logger = get_system_logger(cfg)
    traffic_logger = TrafficLogger(cfg)

    logger.info("Nyx starting...")
    controller = Controller(cfg)

    stop_event = threading.Event()

    # --- Proxy: always-on daemon, started once here, independent of mode.
    # Firewall rules for it are applied by controller.set_mode() below
    # (step 5 of the required order) -- not here directly, to avoid
    # inserting the same iptables rules twice. ---
    try:
        firewall.lockdown_only(cfg)  # start from a known-safe default-deny state
        proxy_manager.start_proxy(cfg)
        logger.info("Always-on proxy service started (firewall rules applied on first mode switch)")
    except Exception as e:
        logger.error(f"Failed to start always-on proxy: {e}")

    # --- Background threads ---
    monitor_thread = threading.Thread(
        target=monitor_loop, args=(cfg, controller, traffic_logger, stop_event), daemon=True
    )
    monitor_thread.start()

    ipc_thread = threading.Thread(target=ipc_server, args=(controller, stop_event), daemon=True)
    ipc_thread.start()

    rotation_scheduler = VPNRotationScheduler(cfg, controller, stop_event)
    rotation_thread = threading.Thread(target=rotation_scheduler.run, daemon=True)
    rotation_thread.start()

    watchdog = KillSwitchWatchdog(cfg, controller, stop_event)
    watchdog_thread = threading.Thread(target=watchdog.run, daemon=True)
    watchdog_thread.start()

    # --- Bring up default mode ---
    default_mode = cfg.get("default_mode", "vpn")
    try:
        controller.set_mode(default_mode)
    except ModeSwitchError as e:
        logger.error(f"Failed to set default mode '{default_mode}' on startup: {e}")

    logger.info(f"IPC socket listening at {SOCKET_PATH}")
    logger.info("Daemon running. Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        stop_event.set()
        controller.emergency_stop()
        proxy_manager.stop_proxy(cfg)
        monitor_thread.join(timeout=3)
        ipc_thread.join(timeout=3)
        rotation_thread.join(timeout=3)
        watchdog_thread.join(timeout=3)


if __name__ == "__main__":
    main()
