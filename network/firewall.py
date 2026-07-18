"""
Firewall / kill switch / always-on proxy chain.

Split into three explicit steps so Controller.set_mode() can enforce the
REQUIRED ORDER exactly:

    1. lockdown_only()        -- block everything, no mode rules yet
    2. (controller stops old tunnel, starts new tunnel)
    3. apply_routing_rules()  -- mode-specific ACCEPT/NAT, now that the
                                  new interface actually exists
    4. enable_proxy_chain()   -- re-apply the always-on proxy layer
                                  (needed because lockdown_only() flushed
                                  the tables, wiping the previous proxy rules)

Previously these were bundled into one enable_kill_switch() call made
BEFORE the new tunnel was brought up -- not a leak (default-deny means a
rule referencing a not-yet-existing interface is inert), but the ordering
didn't match spec and left dead rules around briefly. This version
enforces the literal required order.

UDP note (addresses "redsocks is TCP-only, doesn't that leak UDP?"):
No -- default OUTPUT/FORWARD policy is DROP. redsocks' REDIRECT rule only
matches `-p tcp`, so UDP traffic that isn't explicitly ACCEPTed elsewhere
has NO matching rule and is dropped, not silently sent unproxied. In VPN
mode, UDP client traffic IS explicitly allowed via the protocol-agnostic
FORWARD ACCEPT on the tunnel interface (fine, it goes through the tunnel).
In Tor mode, UDP other than DNS (which we redirect explicitly) has no
matching rule and is dropped -- apps using UDP (VoIP, games, QUIC/HTTP3)
will fail to connect rather than bypass Tor unprotected. That's the
correct fail-closed behavior, just worth knowing since it affects which
apps work in which mode.
"""

import subprocess
import logging

logger = logging.getLogger("nyx.system")


def _run(cmd: list, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)} :: {result.stderr.strip()}")
        raise RuntimeError(f"iptables command failed: {' '.join(cmd)} -> {result.stderr.strip()}")
    return result


def _flush_all():
    for table in ["filter", "nat", "mangle"]:
        _run(["sudo", "iptables", "-t", table, "-F"], check=False)
        _run(["sudo", "iptables", "-t", table, "-X"], check=False)


def _block_ipv6():
    for chain in ["INPUT", "OUTPUT", "FORWARD"]:
        _run(["sudo", "ip6tables", "-P", chain, "DROP"], check=False)


def lockdown_only(cfg):
    """STEP 1 of the required order: block everything. No mode-specific
    rules, no proxy chain -- just default-deny + the bare minimum to not
    lock yourself out (loopback, established connections, SSH from LAN)."""
    net = cfg["network"]
    lan_if = net["lan_interface"]
    mgmt_port = net["management_port"]
    lan_subnet = net["lan_subnet"]

    logger.info("STEP 1: Locking down firewall (default-deny, no mode rules yet)")
    _flush_all()

    _run(["sudo", "iptables", "-P", "INPUT", "DROP"])
    _run(["sudo", "iptables", "-P", "FORWARD", "DROP"])
    _run(["sudo", "iptables", "-P", "OUTPUT", "DROP"])

    _run(["sudo", "iptables", "-A", "INPUT", "-i", "lo", "-j", "ACCEPT"])
    _run(["sudo", "iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"])

    for chain in ["INPUT", "OUTPUT", "FORWARD"]:
        _run(["sudo", "iptables", "-A", chain,
              "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])

    _run(["sudo", "iptables", "-A", "INPUT", "-i", lan_if, "-p", "tcp",
          "--dport", str(mgmt_port), "-s", lan_subnet, "-j", "ACCEPT"])
    _run(["sudo", "iptables", "-A", "OUTPUT", "-o", lan_if, "-p", "tcp",
          "--sport", str(mgmt_port), "-d", lan_subnet, "-j", "ACCEPT"])

    if cfg["firewall"].get("block_ipv6", True):
        _block_ipv6()


def enable_proxy_chain(cfg):
    """STEP 4 of the required order (also called once at boot by main.py).
    Always-on: redirect the Pi's own outbound TCP through redsocks.
        iptables -t nat -A OUTPUT -p tcp -j REDIRECT --to-ports 12345
    with an owner-match exclusion so redsocks' own upstream connection
    doesn't get redirected into itself."""
    proxy_port = cfg["proxy"]["redsocks_port"]
    wan_if = cfg["network"]["wan_interface"]

    logger.info(f"STEP 4: Re-applying always-on proxy chain -> redsocks:{proxy_port}")

    _run(["sudo", "iptables", "-t", "nat", "-A", "OUTPUT",
          "-m", "owner", "--uid-owner", "redsocks", "-j", "RETURN"])
    _run(["sudo", "iptables", "-t", "nat", "-A", "OUTPUT",
          "-d", "127.0.0.0/8", "-j", "RETURN"])
    _run(["sudo", "iptables", "-t", "nat", "-A", "OUTPUT",
          "-p", "tcp", "-j", "REDIRECT", "--to-ports", str(proxy_port)])
    _run(["sudo", "iptables", "-A", "OUTPUT", "-m", "owner",
          "--uid-owner", "redsocks", "-o", wan_if, "-j", "ACCEPT"])
    _run(["sudo", "iptables", "-A", "OUTPUT", "-o", "lo", "-p", "tcp",
          "--dport", str(proxy_port), "-j", "ACCEPT"])


def _apply_dns_leak_protection(cfg, redirect_to_local_port=None):
    if not cfg["firewall"].get("dns_leak_protection", True):
        return

    net = cfg["network"]
    lan_if = net["lan_interface"]

    if redirect_to_local_port:
        _run(["sudo", "iptables", "-t", "nat", "-A", "PREROUTING", "-i", lan_if,
              "-p", "udp", "--dport", "53", "-j", "REDIRECT",
              "--to-ports", str(redirect_to_local_port)])
        _run(["sudo", "iptables", "-t", "nat", "-A", "PREROUTING", "-i", lan_if,
              "-p", "tcp", "--dport", "53", "-j", "REDIRECT",
              "--to-ports", str(redirect_to_local_port)])
        _run(["sudo", "iptables", "-A", "INPUT", "-i", lan_if, "-p", "udp",
              "--dport", str(redirect_to_local_port), "-j", "ACCEPT"])
        _run(["sudo", "iptables", "-A", "INPUT", "-i", lan_if, "-p", "tcp",
              "--dport", str(redirect_to_local_port), "-j", "ACCEPT"])


def _apply_self_dns_protection(cfg, mode: str, vpn_profile: dict = None):
    """DNS protection for the Pi's OWN queries (ip_monitor.py, apt, etc.) --
    previously MISSING entirely. Client DNS is handled by
    _apply_dns_leak_protection() (PREROUTING from the LAN interface); this
    covers the separate OUTPUT-chain case of the Pi itself doing a lookup.
    Under default-deny, without this, the Pi's own DNS resolution simply
    fails silently (not a leak, but broken) -- there was no ACCEPT or
    REDIRECT for its own port-53 traffic at all before this fix."""
    if not cfg["firewall"].get("dns_leak_protection", True):
        return

    if mode == "tor":
        dns_port = cfg["tor"]["dns_port"]
        # Redirect the Pi's own DNS queries into Tor's DNSPort, same as
        # client DNS in tor mode -- consistent protection, not a special case.
        _run(["sudo", "iptables", "-t", "nat", "-A", "OUTPUT", "-p", "udp",
              "--dport", "53", "-j", "REDIRECT", "--to-ports", str(dns_port)])
        _run(["sudo", "iptables", "-A", "OUTPUT", "-o", "lo", "-p", "udp",
              "--dport", str(dns_port), "-j", "ACCEPT"])
    elif mode == "vpn":
        vpn_if = vpn_profile["interface"]
        # Only allow the Pi's own DNS out via the tunnel interface -- same
        # principle as the client-facing rule: no path out = no leak,
        # assuming your VPN profile routes a resolver through the tunnel.
        _run(["sudo", "iptables", "-A", "OUTPUT", "-p", "udp", "--dport", "53",
              "-o", vpn_if, "-j", "ACCEPT"])


def apply_routing_rules(cfg, mode: str, vpn_profile: dict = None):
    """STEP 3 of the required order: called AFTER the new tunnel is up and
    verified, so these rules reference a live interface, not a
    not-yet-existing one."""
    logger.info(f"STEP 3: Applying routing rules for mode '{mode}'")

    net = cfg["network"]
    lan_if = net["lan_interface"]
    wan_if = net["wan_interface"]

    if mode == "vpn":
        if not vpn_profile:
            raise ValueError("vpn_profile is required for mode='vpn'")
        vpn_if = vpn_profile["interface"]
        _run(["sudo", "iptables", "-A", "OUTPUT", "-o", vpn_if, "-j", "ACCEPT"])
        _run(["sudo", "iptables", "-A", "FORWARD", "-i", lan_if, "-o", vpn_if, "-j", "ACCEPT"])
        _run(["sudo", "iptables", "-A", "FORWARD", "-i", vpn_if, "-o", lan_if, "-j", "ACCEPT"])
        _run(["sudo", "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", vpn_if, "-j", "MASQUERADE"])
        _apply_dns_leak_protection(cfg, redirect_to_local_port=None)
        _apply_self_dns_protection(cfg, mode, vpn_profile=vpn_profile)

    elif mode == "tor":
        tor_cfg = cfg["tor"]
        _run(["sudo", "iptables", "-t", "nat", "-A", "PREROUTING", "-i", lan_if,
              "-p", "tcp", "--syn", "-j", "REDIRECT",
              "--to-ports", str(tor_cfg["trans_port"])])
        _run(["sudo", "iptables", "-A", "INPUT", "-i", lan_if, "-p", "tcp",
              "--dport", str(tor_cfg["trans_port"]), "-j", "ACCEPT"])
        _run(["sudo", "iptables", "-A", "OUTPUT", "-m", "owner", "--uid-owner", "debian-tor",
              "-o", wan_if, "-j", "ACCEPT"])
        _apply_dns_leak_protection(cfg, redirect_to_local_port=tor_cfg["dns_port"])
        _apply_self_dns_protection(cfg, mode, vpn_profile=vpn_profile)

    else:
        raise ValueError(f"Unknown mode: {mode} (only 'vpn' and 'tor' are valid modes)")


def disable_kill_switch(cfg):
    logger.warning("Kill switch DISABLED — traffic is unprotected (emergency stop only)")
    _flush_all()
    _run(["sudo", "iptables", "-P", "INPUT", "ACCEPT"])
    _run(["sudo", "iptables", "-P", "FORWARD", "ACCEPT"])
    _run(["sudo", "iptables", "-P", "OUTPUT", "ACCEPT"])


def verify_no_leak(cfg, mode: str, vpn_profile: dict = None) -> dict:
    result = _run(["sudo", "iptables", "-S"], check=False)
    rules = result.stdout

    checks = {
        "forward_default_drop": "-P FORWARD DROP" in rules,
        "output_default_drop": "-P OUTPUT DROP" in rules,
        "proxy_chain_present": str(cfg["proxy"]["redsocks_port"]) in rules,
    }

    if mode == "vpn":
        vpn_if = vpn_profile["interface"] if vpn_profile else cfg["vpn"]["profiles"][0]["interface"]
        checks["vpn_forward_rule_present"] = f"-o {vpn_if}" in rules
    elif mode == "tor":
        checks["tor_redirect_present"] = str(cfg["tor"]["trans_port"]) in rules

    return checks


def tunnel_interface_alive(interface: str) -> bool:
    result = subprocess.run(["ip", "link", "show", interface],
                             capture_output=True, text=True)
    return result.returncode == 0 and "UP" in result.stdout


def dump_active_rules() -> dict:
    """Debug helper -- 'if things break, run this' (see README)."""
    filt = _run(["sudo", "iptables", "-S"], check=False).stdout
    nat = _run(["sudo", "iptables", "-t", "nat", "-S"], check=False).stdout
    return {"filter": filt, "nat": nat}
