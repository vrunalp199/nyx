"""
Central logging. Two logs:
  - system.log  -> mode switches, errors, service start/stop (human events)
  - traffic.log -> periodic IP/bandwidth snapshots (machine-parseable, JSON lines)

Using stdlib logging (not print()) so log level, rotation, and destinations
are all controllable from config.yaml instead of hardcoded.
"""

import logging
import json
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


def _ensure_dir(path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def get_system_logger(cfg):
    log_path = cfg["logging"]["system_log"]
    _ensure_dir(log_path)

    logger = logging.getLogger("nyx.system")
    if logger.handlers:
        return logger  # already configured, avoid duplicate handlers

    level = getattr(logging, cfg["logging"].get("level", "INFO"))
    logger.setLevel(level)

    fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


class TrafficLogger:
    """Appends JSON lines: one snapshot per poll interval. Kept separate from
    system.log because traffic data is high-volume and machine-read (e.g. by
    the dashboard charts), while system.log is for humans debugging events."""

    def __init__(self, cfg):
        self.path = cfg["logging"]["traffic_log"]
        _ensure_dir(self.path)

    def log(self, ip_info: dict, bandwidth: dict, mode: str):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "ip": ip_info,
            "bandwidth": bandwidth,
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
