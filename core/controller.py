"""
Core controller. Enforces the REQUIRED ORDER for every mode switch:

    1. firewall.lockdown_only()        -- block everything
    2. stop old tunnel
    3. start new tunnel, verify it's actually up
    4. firewall.apply_routing_rules()  -- mode-specific ACCEPT/NAT, now
                                           that the interface is live
    5. firewall.enable_proxy_chain()   -- re-apply always-on proxy layer
    6. verify_no_leak()                -- fail closed if anything's wrong

If step 3 fails, we never reach steps 4/5 -- firewall stays at
lockdown_only() (block everything), which is fail-closed by construction.

Rotation safety: rotate_vpn() runs vpn_manager.preflight_check() on the
next profile BEFORE tearing down the current (working) tunnel. If
preflight fails, we skip this rotation cycle and stay on the current
profile -- no disruption. Preflight can't fully verify a WireGuard
handshake without bringing the interface up (see vpn_manager.py docstring
for why), so set_mode() can still fail after preflight passes; for THAT
case, _rollback_after_failed_rotation() restores the last known-good
profile (bounded retries) rather than leaving the Pi stuck offline.

Thread safety: core/scheduler.py (rotation) and core/watchdog.py (drop
detection) both run as separate threads and both call into these methods.
self._mode_lock (RLock, since rollback methods call set_mode() from
inside another locked method on the same thread) makes mode switching,
rotation, and emergency stop mutually exclusive -- without it, a rotation
and a watchdog-triggered reconnect could interleave iptables commands
from two threads and corrupt the rule set mid-write.
"""

import logging
import random
import time
import threading

from network import vpn_manager, tor_manager, firewall, routing

logger = logging.getLogger("nyx.system")

VALID_MODES = ["vpn", "tor"]


class ModeSwitchError(Exception):
    pass


class Controller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.current_mode = None
        self.current_vpn_profile = None
        self.last_ip_info = {}
        self.last_bandwidth = {}
        self.last_snapshot_time = None
        self.bandwidth_rate = {"sent_bps": 0, "recv_bps": 0}
        self.last_resource_snapshot = {}
        self.emergency_stopped = False
        self.last_known_good = None       # {"mode": ..., "profile": ...}
        self.rotation_degraded = False
        self.last_mode_change_time = None
        self.last_rotation_time = None
        self._mode_lock = threading.RLock()
        routing.enable_ip_forwarding()
        routing.disable_ipv6_stack(cfg)

    # ------------------------------------------------------------------
    # Mode switching -- enforces the required order exactly
    # ------------------------------------------------------------------

    def set_mode(self, mode: str, vpn_profile: dict = None):
        with self._mode_lock:
            if mode not in VALID_MODES:
                raise ValueError(f"Unknown mode '{mode}'. Valid: {VALID_MODES} "
                                  f"(proxy is always-on, not a selectable mode)")

            logger.info(f"Switching mode: {self.current_mode} -> {mode}")
            self.emergency_stopped = False

            profile = vpn_profile or (vpn_manager.get_profiles(self.cfg)[0] if mode == "vpn" else None)

            # STEP 1: block everything
            if self.cfg["firewall"].get("kill_switch", True):
                firewall.lockdown_only(self.cfg)

            # STEP 2: stop old tunnel
            self._stop_tunnels(except_mode=mode)

            # STEP 3: start new tunnel, verify
            try:
                if mode == "vpn":
                    vpn_manager.start_vpn(self.cfg, profile)
                    up = vpn_manager.is_vpn_up(self.cfg, profile)
                elif mode == "tor":
                    tor_manager.start_tor(self.cfg)
                    up = tor_manager.is_tor_running(self.cfg)
            except Exception as e:
                logger.error(f"Failed to bring up mode '{mode}': {e}. "
                             f"Firewall remains at lockdown_only() -- fail closed.")
                raise ModeSwitchError(f"Could not start '{mode}': {e}") from e

            if not up:
                logger.error(f"Mode '{mode}' service did not come up cleanly. "
                             f"Firewall remains at lockdown_only() -- fail closed.")
                raise ModeSwitchError(f"'{mode}' service failed verification after start")

            # STEP 4: routing rules (interface is live now)
            if self.cfg["firewall"].get("kill_switch", True):
                firewall.apply_routing_rules(self.cfg, mode, vpn_profile=profile)

            # STEP 5: re-enable always-on proxy chain
            if self.cfg["firewall"].get("kill_switch", True):
                firewall.enable_proxy_chain(self.cfg)

            # STEP 6: verify
            leak_checks = firewall.verify_no_leak(self.cfg, mode, vpn_profile=profile)
            if not all(leak_checks.values()):
                failed = [k for k, v in leak_checks.items() if not v]
                logger.error(f"Leak check failed for mode '{mode}': {failed}")
                raise ModeSwitchError(f"Firewall verification failed: {failed}")

            self.current_mode = mode
            self.current_vpn_profile = profile if mode == "vpn" else None
            self.last_mode_change_time = time.time()
            self.last_known_good = {"mode": mode, "profile": profile}

            logger.info(f"Mode switched successfully to '{mode}'"
                        + (f" (profile: {profile['name']})" if profile else ""))
            return {"mode": mode, "profile": profile["name"] if profile else None,
                    "kill_switch": True, "checks": leak_checks}

    def _stop_tunnels(self, except_mode=None):
        if except_mode != "vpn":
            vpn_manager.stop_all_vpn_profiles(self.cfg)
        if except_mode != "tor":
            tor_manager.stop_tor(self.cfg)

    # ------------------------------------------------------------------
    # VPN rotation: preflight check, then rollback-on-failure as backstop
    # ------------------------------------------------------------------

    def rotate_vpn(self):
        with self._mode_lock:
            if self.current_mode != "vpn":
                logger.debug("rotate_vpn() called but current mode isn't vpn -- skipping")
                return None

            profiles = vpn_manager.get_profiles(self.cfg)
            if len(profiles) < 2:
                logger.warning("VPN rotation requested but fewer than 2 profiles configured -- skipping")
                return None

            current_name = self.current_vpn_profile["name"] if self.current_vpn_profile else None
            current_idx = next((i for i, p in enumerate(profiles) if p["name"] == current_name), -1)
            next_profile = profiles[(current_idx + 1) % len(profiles)]

            # Preflight BEFORE touching the currently-working tunnel.
            ok, reason = vpn_manager.preflight_check(next_profile)
            if not ok:
                logger.warning(f"Skipping rotation to '{next_profile['name']}': "
                               f"preflight failed ({reason}). Staying on '{current_name}'.")
                return None

            logger.info(f"Rotating VPN: {current_name} -> {next_profile['name']} (preflight ok)")
            try:
                result = self.set_mode("vpn", vpn_profile=next_profile)
                self.last_rotation_time = time.time()
                self.rotation_degraded = False
                return result
            except ModeSwitchError as e:
                logger.error(f"Rotation to '{next_profile['name']}' failed despite passing "
                             f"preflight: {e}. Attempting rollback to last known-good profile...")
                return self._rollback_after_failed_rotation()

    def _rollback_after_failed_rotation(self, max_attempts=2):
        if not self.last_known_good or self.last_known_good.get("mode") != "vpn":
            logger.critical("No known-good VPN profile to roll back to -- "
                             "staying blocked (fail closed). Manual intervention required.")
            self.rotation_degraded = True
            return None

        good_profile = self.last_known_good["profile"]
        for attempt in range(1, max_attempts + 1):
            try:
                logger.warning(f"Rollback attempt {attempt}/{max_attempts}: "
                               f"restoring '{good_profile['name']}'")
                result = self.set_mode("vpn", vpn_profile=good_profile)
                self.rotation_degraded = False
                logger.info(f"Rollback succeeded -- back on '{good_profile['name']}'")
                return result
            except ModeSwitchError as e:
                logger.error(f"Rollback attempt {attempt}/{max_attempts} failed: {e}")

        logger.critical(f"All {max_attempts} rollback attempts failed. Remaining in "
                        f"fail-closed blocked state -- no traffic flows until this "
                        f"is fixed manually (check VPN configs / connectivity).")
        self.rotation_degraded = True
        return None

    def rotate_vpn_random(self):
        with self._mode_lock:
            if self.current_mode != "vpn":
                return None
            profiles = vpn_manager.get_profiles(self.cfg)
            choices = [p for p in profiles if not self.current_vpn_profile or p["name"] != self.current_vpn_profile["name"]]
            if not choices:
                return None
            candidate = random.choice(choices)
            ok, reason = vpn_manager.preflight_check(candidate)
            if not ok:
                logger.warning(f"Skipping random rotation to '{candidate['name']}': {reason}")
                return None
            try:
                result = self.set_mode("vpn", vpn_profile=candidate)
                self.last_rotation_time = time.time()
                return result
            except ModeSwitchError:
                return self._rollback_after_failed_rotation()

    # ------------------------------------------------------------------
    # Kill-switch watchdog support
    # ------------------------------------------------------------------

    def check_tunnel_health(self) -> bool:
        if self.current_mode == "vpn" and self.current_vpn_profile:
            return firewall.tunnel_interface_alive(self.current_vpn_profile["interface"])
        elif self.current_mode == "tor":
            return tor_manager.is_tor_running(self.cfg)
        return True

    def reconnect_current_mode(self):
        with self._mode_lock:
            if self.current_mode == "vpn":
                logger.warning("Attempting VPN reconnect after detected drop...")
                try:
                    return self.set_mode("vpn", vpn_profile=self.current_vpn_profile)
                except ModeSwitchError:
                    return self._rollback_after_failed_rotation()
            elif self.current_mode == "tor":
                logger.warning("Attempting Tor reconnect after detected drop...")
                return self.set_mode("tor")

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    def emergency_stop(self):
        with self._mode_lock:
            logger.warning("EMERGENCY STOP requested -- disabling kill switch and stopping all tunnels")
            self._stop_tunnels()
            firewall.disable_kill_switch(self.cfg)
            self.current_mode = None
            self.current_vpn_profile = None
            self.emergency_stopped = True
            return {"mode": None, "kill_switch": False, "emergency_stopped": True}

    # ------------------------------------------------------------------
    # Snapshots for the live dashboard (single monitor-loop thread writes
    # these; no lock needed for these specific fields since dict/float
    # assignment is atomic under the GIL and there's only one writer)
    # ------------------------------------------------------------------

    def update_snapshot(self, ip_info: dict, bandwidth: dict):
        now = time.time()
        if self.last_snapshot_time and self.last_bandwidth:
            dt = now - self.last_snapshot_time
            if dt > 0:
                self.bandwidth_rate = {
                    "sent_bps": max(0, (bandwidth["sent_bytes"] - self.last_bandwidth["sent_bytes"]) / dt),
                    "recv_bps": max(0, (bandwidth["recv_bytes"] - self.last_bandwidth["recv_bytes"]) / dt),
                }
        self.last_ip_info = ip_info
        self.last_bandwidth = bandwidth
        self.last_snapshot_time = now

    def update_resource_snapshot(self, snapshot: dict):
        self.last_resource_snapshot = snapshot

    def status(self):
        rotation_cfg = self.cfg["vpn"]["rotation"]
        next_rotation_eta = None
        if (self.current_mode == "vpn" and rotation_cfg.get("enabled", False)
                and self.last_rotation_time):
            elapsed = time.time() - self.last_rotation_time
            next_rotation_eta = max(0, rotation_cfg["interval_seconds"] - elapsed)

        return {
            "current_mode": self.current_mode,
            "current_vpn_profile": self.current_vpn_profile["name"] if self.current_vpn_profile else None,
            "tor_running": tor_manager.is_tor_running(self.cfg),
            "ip_forwarding": routing.is_ip_forwarding_enabled(),
            "emergency_stopped": self.emergency_stopped,
            "traffic_blocked_by_default": self.current_mode is None,
            "ip_info": self.last_ip_info,
            "bandwidth": self.last_bandwidth,
            "bandwidth_rate": self.bandwidth_rate,
            "rotation_enabled": rotation_cfg.get("enabled", False),
            "rotation_interval_seconds": rotation_cfg.get("interval_seconds"),
            "last_rotation_time": self.last_rotation_time,
            "next_rotation_eta_seconds": next_rotation_eta,
            "rotation_degraded": self.rotation_degraded,
            "resource": self.last_resource_snapshot,
        }
