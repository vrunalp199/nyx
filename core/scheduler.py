"""
VPN rotation scheduler. Runs the rotation on the configured interval, but
retries SOONER (not the full interval) after a failed rotation, so a
transient failure doesn't leave the router degraded for the full 10
minutes (or whatever X is configured) before trying again.
"""

import logging
import threading

logger = logging.getLogger("nyx.system")

RETRY_INTERVAL_SECONDS = 30  # how soon to retry after a failed rotation


class VPNRotationScheduler:
    def __init__(self, cfg, controller, stop_event: threading.Event):
        self.cfg = cfg
        self.controller = controller
        self.stop_event = stop_event

    def run(self):
        rotation_cfg = self.cfg["vpn"]["rotation"]
        interval = rotation_cfg.get("interval_seconds", 600)
        logger.info(f"VPN rotation scheduler started (interval={interval}s, "
                    f"enabled={rotation_cfg.get('enabled', False)})")

        while not self.stop_event.is_set():
            # If the last rotation attempt left us degraded, retry sooner
            # than the full interval instead of waiting it out.
            wait_time = RETRY_INTERVAL_SECONDS if self.controller.rotation_degraded else interval
            self.stop_event.wait(wait_time)
            if self.stop_event.is_set():
                break

            if not self.cfg["vpn"]["rotation"].get("enabled", False):
                continue
            if self.controller.current_mode != "vpn" and not self.controller.rotation_degraded:
                continue

            try:
                self.controller.rotate_vpn()
            except Exception as e:
                logger.error(f"VPN rotation failed: {e}")
