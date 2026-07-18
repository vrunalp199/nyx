"""
IP forwarding control. Required for the Pi to act as a router at all --
without this, no amount of correct iptables rules will forward client
traffic anywhere (this was missing entirely from the original prototype).
"""

import subprocess
import logging

logger = logging.getLogger("nyx.system")

IPV4_FORWARD_PATH = "/proc/sys/net/ipv4/ip_forward"


def enable_ip_forwarding():
    logger.info("Enabling IPv4 forwarding...")
    result = subprocess.run(
        ["sudo", "sysctl", "-w", "net.ipv4.ip_forward=1"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error(f"Failed to enable IP forwarding: {result.stderr.strip()}")
        raise RuntimeError(result.stderr.strip())


def disable_ipv6_stack(cfg):
    """Belt-and-suspenders alongside firewall.py's `ip6tables -P ... DROP`:
    that blocks IPv6 packets at the firewall layer, this disables the
    stack at the kernel level entirely. Two layers because ip6tables rules
    can be flushed/reset by other tooling or a race during a mode switch;
    the sysctl-level disable doesn't depend on firewall rule state at all.
    Only runs if firewall.block_ipv6 is true in config."""
    if not cfg["firewall"].get("block_ipv6", True):
        return
    logger.info("Disabling IPv6 stack at the kernel level (sysctl)...")
    for setting in ["net.ipv6.conf.all.disable_ipv6=1",
                    "net.ipv6.conf.default.disable_ipv6=1",
                    "net.ipv6.conf.lo.disable_ipv6=1"]:
        result = subprocess.run(["sudo", "sysctl", "-w", setting],
                                 capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(f"Could not apply '{setting}': {result.stderr.strip()}")


def disable_ip_forwarding():
    logger.info("Disabling IPv4 forwarding...")
    subprocess.run(["sudo", "sysctl", "-w", "net.ipv4.ip_forward=0"],
                    capture_output=True, text=True)


def is_ip_forwarding_enabled() -> bool:
    try:
        with open(IPV4_FORWARD_PATH) as f:
            return f.read().strip() == "1"
    except FileNotFoundError:
        return False
