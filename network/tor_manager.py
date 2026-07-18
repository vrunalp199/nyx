"""
Tor service control. Actual transparent routing (TransPort/DNSPort
redirection) is handled by firewall.py -- this module only owns the
service lifecycle, keeping "start the daemon" separate from "route
traffic into it" (the original prototype conflated these, which is why
'Tor mode' didn't actually route anything).
"""

import subprocess
import logging

logger = logging.getLogger("nyx.system")


class TorError(Exception):
    pass


def start_tor(cfg) -> bool:
    logger.info("Starting Tor service...")
    result = subprocess.run(["sudo", "systemctl", "start", "tor"],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to start Tor: {result.stderr.strip()}")
        raise TorError(result.stderr.strip())
    logger.info("Tor service started")
    return True


def stop_tor(cfg) -> bool:
    logger.info("Stopping Tor service...")
    result = subprocess.run(["sudo", "systemctl", "stop", "tor"],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"Tor stop returned non-zero: {result.stderr.strip()}")
        return False
    logger.info("Tor service stopped")
    return True


def is_tor_running(cfg) -> bool:
    result = subprocess.run(["systemctl", "is-active", "tor"],
                             capture_output=True, text=True)
    return result.stdout.strip() == "active"
