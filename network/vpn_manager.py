"""
WireGuard VPN control with multi-profile support (needed for rotation).
Each profile is {name, interface, config_path}. Only one profile is ever
up at a time.
"""

import subprocess
import socket
import re
import os
import logging

logger = logging.getLogger("nyx.system")


class VPNError(Exception):
    pass


def get_profiles(cfg):
    return cfg["vpn"]["profiles"]


def preflight_check(profile: dict) -> tuple:
    """Best-effort validation BEFORE switching to this profile, so a
    rotation doesn't blindly tear down a working tunnel for a broken one.

    Checks:
      1. Config file exists and has the required WireGuard keys
      2. The Endpoint host resolves via DNS

    Honest limitation: this does NOT verify the WireGuard handshake
    actually succeeds. Doing that without disruption would require
    bringing the new interface up in parallel (e.g. via a separate network
    namespace) while the old one is still active -- a heavier change than
    this version makes. This preflight catches the common failure modes
    (typo'd path, expired/wrong config, DNS-dead endpoint host) cheaply,
    without touching the currently-active tunnel. A profile that passes
    preflight can still fail to actually connect; Controller's rollback
    logic is the backstop for that case.

    Returns (ok: bool, reason: str)
    """
    config_path = profile["config_path"]

    if not os.path.exists(config_path):
        return False, f"config file not found: {config_path}"

    try:
        with open(config_path) as f:
            content = f.read()
    except OSError as e:
        return False, f"could not read config file: {e}"

    for required in ["PrivateKey", "PublicKey", "Endpoint"]:
        if required not in content:
            return False, f"config missing required field: {required}"

    match = re.search(r"Endpoint\s*=\s*([^\s:]+):(\d+)", content)
    if not match:
        return False, "could not parse Endpoint host:port from config"

    endpoint_host = match.group(1)
    try:
        socket.getaddrinfo(endpoint_host, None, family=socket.AF_UNSPEC)
    except socket.gaierror as e:
        return False, f"Endpoint host '{endpoint_host}' does not resolve: {e}"

    return True, "ok"


def start_vpn(cfg, profile: dict) -> bool:
    vpn_if = profile["interface"]
    logger.info(f"Starting VPN profile '{profile['name']}' ({vpn_if})...")
    result = subprocess.run(["sudo", "wg-quick", "up", vpn_if],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Failed to start VPN profile '{profile['name']}': {result.stderr.strip()}")
        raise VPNError(result.stderr.strip())
    logger.info(f"VPN profile '{profile['name']}' is up on {vpn_if}")
    return True


def stop_vpn(cfg, profile: dict) -> bool:
    vpn_if = profile["interface"]
    logger.info(f"Stopping VPN profile '{profile['name']}' ({vpn_if})...")
    result = subprocess.run(["sudo", "wg-quick", "down", vpn_if],
                             capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"VPN stop returned non-zero (may already be down): {result.stderr.strip()}")
        return False
    logger.info(f"VPN profile '{profile['name']}' is down")
    return True


def stop_all_vpn_profiles(cfg):
    """Used when switching to tor mode, or before rotating -- makes sure no
    stray WireGuard interface is left up from a previous profile."""
    for profile in get_profiles(cfg):
        stop_vpn(cfg, profile)


def is_vpn_up(cfg, profile: dict) -> bool:
    vpn_if = profile["interface"]
    result = subprocess.run(["ip", "link", "show", vpn_if],
                             capture_output=True, text=True)
    return result.returncode == 0
