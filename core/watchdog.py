"""
Kill-switch watchdog. This is #4 from the review: "if VPN drops, real IP
leaks." The iptables rules already fail closed by construction (default
DROP + narrow interface-specific ACCEPT means a dead interface has no
matching rule left to allow traffic through) -- but that's a passive
guarantee. This watchdog is the ACTIVE half: it notices the drop quickly,
logs it clearly, and attempts reconnection, instead of you finding out
your VPN died three hours ago only because you happened to check.
"""

import logging
import threading

logger = logging.getLogger("nyx.system")


class KillSwitchWatchdog:
    def __init__(self, cfg, controller, stop_event: threading.Event):
        self.cfg = cfg
        self.controller = controller
        self.stop_event = stop_event
        self.consecutive_failures = 0

    def run(self):
        interval = self.cfg["firewall"].get("watchdog_interval_seconds", 3)
        logger.info(f"Kill-switch watchdog started (checking every {interval}s)")

        while not self.stop_event.is_set():
            self.stop_event.wait(interval)
            if self.stop_event.is_set():
                break

            if self.controller.current_mode is None or self.controller.emergency_stopped:
                continue  # nothing active to watch

            healthy = self.controller.check_tunnel_health()

            if healthy:
                if self.consecutive_failures > 0:
                    logger.info("Tunnel health restored")
                self.consecutive_failures = 0
                continue

            self.consecutive_failures += 1
            logger.critical(
                f"KILL SWITCH WATCHDOG: '{self.controller.current_mode}' tunnel appears DOWN "
                f"(failure #{self.consecutive_failures}). Client traffic should already be "
                f"blocked by the FORWARD-chain default-drop rule. Attempting reconnect..."
            )

            try:
                self.controller.reconnect_current_mode()
                logger.info("Reconnect attempt completed")
                self.consecutive_failures = 0
            except Exception as e:
                logger.error(f"Reconnect attempt failed: {e}. Traffic remains blocked "
                              f"(fail-closed) until this succeeds or you intervene manually.")
