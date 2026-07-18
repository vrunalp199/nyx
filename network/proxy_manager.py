"""
Redsocks (transparent proxy) service control. Same split as tor_manager.py:
this owns start/stop only, firewall.py owns the NAT redirect rules.
"""

import subprocess
import logging

logger = logging.getLogger("nyx.system")


class ProxyError(Exception):
    pass


def start_proxy(cfg) -> bool:
    logger.info("Starting redsocks proxy service...")
    result = subprocess.run(["sudo", "systemctl", "start", "redsocks"],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to start redsocks: {result.stderr.strip()}")
        raise ProxyError(result.stderr.strip())
    logger.info("redsocks started")
    return True


def stop_proxy(cfg) -> bool:
    logger.info("Stopping redsocks proxy service...")
    result = subprocess.run(["sudo", "systemctl", "stop", "redsocks"],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"redsocks stop returned non-zero: {result.stderr.strip()}")
        return False
    logger.info("redsocks stopped")
    return True


def is_proxy_running(cfg) -> bool:
    result = subprocess.run(["systemctl", "is-active", "redsocks"],
                             capture_output=True, text=True)
    return result.stdout.strip() == "active"
