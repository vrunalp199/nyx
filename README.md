<p align="center">
  <img src="assets/banner.svg" alt="Nyx — privacy-first router for Raspberry Pi" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Raspberry%20Pi%20CM4-a4133c?style=flat-square" alt="platform">
  <img src="https://img.shields.io/badge/python-3.9+-2dd4bf?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-8892bf?style=flat-square" alt="license">
  <img src="https://img.shields.io/badge/status-hobby%20project-fb923c?style=flat-square" alt="status">
</p>

# Nyx

A Raspberry Pi that sits between your devices and the internet, forcing
all traffic through a VPN, Tor, or a proxy — with a real kill switch,
DNS leak protection, and a CLI/dashboard to control it.

> Named after the Greek goddess of night — this thing's whole job is to keep your traffic in the dark.

## What changed from v1

| Bug / gap | Fix |
|---|---|
| Kill switch only set `OUTPUT DROP` (governs the Pi's own traffic) | Now locks down `FORWARD` too — that's the chain client-device traffic actually passes through |
| `mode_manager.set_mode()` never called the firewall | `Controller.set_mode()` now applies firewall rules *before* touching any service |
| Starting Tor/redsocks didn't route any traffic | `firewall.py` adds the actual NAT `REDIRECT`/`PREROUTING` rules into TransPort/redsocks port |
| No DNS leak protection | DNS explicitly redirected (Tor) or scoped to the tunnel interface (VPN) |
| `config.yaml` was never read | `core/config_loader.py`, all modules pull settings from it |
| CLI and dashboard would each own a separate `Controller` and fight over iptables | Single daemon (`main.py`) owns the one `Controller`; CLI and dashboard talk to it over a Unix socket |
| Mode-switch leak window (stop old, start new, no lockdown in between) | Firewall lockdown happens first, fail-closed if the new service doesn't come up |
| SSH lockout risk when kill switch enables | Explicit rule preserves management access from the LAN subnet |
| Bare `except:`, `http://` IP lookup, no timeouts | Fixed in `monitor/ip_monitor.py` |
| **Proxy was a 3rd mode, conflicting with VPN/Tor** | Proxy is now always-on (see architecture decision below), only `vpn`/`tor` are modes |
| **No VPN rotation** | `core/scheduler.py` rotates through `vpn.profiles` every `vpn.rotation.interval_seconds`, using the exact same fail-closed `set_mode()` path as a manual switch |
| **No active kill-switch watchdog** | `core/watchdog.py` polls tunnel health every few seconds, logs `CRITICAL` on a drop, attempts reconnect. The block itself was already automatic (dead interface = no matching ACCEPT rule); the watchdog adds detection + recovery |
| **Dashboard not wired to backend** | Now reads real `/api/status` (mode, VPN profile, IP, geolocation, ISP) every 3s, with working mode/rotate/emergency-stop buttons |
| **Mode-switch order didn't literally match spec** | `firewall.py` split into `lockdown_only()` → (stop old / start new) → `apply_routing_rules()` → `enable_proxy_chain()`, called in that exact order by `Controller.set_mode()`. Verified with a call-order assertion test, not just read-through. |
| **Failed rotation could strand you offline** | `rotate_vpn()` now attempts rollback to the last known-good profile (bounded retries) before giving up; only stays blocked if rollback also fails, surfaced via `rotation_degraded` in `/api/status` |
| **No resource visibility** | `monitor/resource_monitor.py` + `system.max_cpu_percent`/`max_mem_percent` in config — logs warnings and surfaces to the dashboard (not a hard throttle, just visibility) |
| **Dashboard lacked rotation/bandwidth/resource info** | Now shows live IP, mode, rotation countdown + last-rotation time, bandwidth rate (computed from consecutive snapshots), CPU/mem, with warning banners for degraded rotation or high resource usage |

### On "redsocks doesn't proxy UDP — leak risk?"

Not a leak in this design, worth being precise about why: default `OUTPUT`/`FORWARD` policy is `DROP`. redsocks' `REDIRECT` rule only matches `-p tcp`. UDP traffic with no other matching rule has nowhere to go and is **dropped**, not sent unprotected. In VPN mode, client UDP is explicitly allowed via the protocol-agnostic `FORWARD ACCEPT -o <vpn_if>` rule (fine — it goes through the tunnel). In Tor mode, non-DNS UDP (VoIP, games, QUIC/HTTP3) has no matching rule and is simply blocked — apps using it will fail to connect rather than bypass Tor. That's the fail-closed default, but it does mean those app *categories* won't work in Tor mode, which is worth knowing going in.

### On the "live" dashboard

This is short-poll (2s interval), not a websocket/SSE push. It reads real data from `Controller.status()` — actual bandwidth rate (computed from two consecutive `psutil` snapshots, not simulated), actual rotation countdown from `vpn.rotation.interval_seconds`, actual CPU/mem from `psutil`. If you want a true push-based live view later, that's a further upgrade (Flask-SocketIO or SSE) — flag it if you want that now instead.

## Architecture decision: proxy is not a mode

You asked for "only VPN and Tor are modes, proxy should always run." Taken
literally, that creates a conflict: a single packet can't be NAT-redirected
to two different destinations (proxy AND VPN, or proxy AND Tor) with plain
iptables `REDIRECT`. I resolved it as:

- **Proxy (redsocks)** is now an **always-on layer for the Pi's own
  outbound traffic** (monitor's IP checks, apt updates, anything the Pi
  itself originates) — lives in the `OUTPUT` chain, chained to your
  configured upstream proxy in `/etc/redsocks.conf`. It's started once at
  boot (`main.py`) and re-applied after every mode switch (since
  `enable_kill_switch()` flushes the tables — see `firewall.py` docstring).
- **VPN / Tor** remain the switchable modes and govern **client-device
  traffic** (your laptop/phone through the Pi) — `FORWARD` chain + NAT
  `PREROUTING`.

If you actually wanted client traffic double-wrapped (e.g. client → proxy
→ VPN, so your ISP sees a proxy connection instead of a raw WireGuard
handshake), that's a heavier build — it means redsocks' upstream target
would need to *be* the VPN's SOCKS capability, which WireGuard doesn't
natively expose (unlike some commercial VPN clients). Tell me if that's
actually what you meant and I'll redesign around it specifically.

## Requirements

- Raspberry Pi (any model with two network paths — e.g. Ethernet in from
  your router, Wi-Fi client devices connect to, or vice versa)
- Raspberry Pi OS (Debian-based)
- Root access (the daemon manages iptables/wg-quick/systemd, all of which
  need root)

## Setup

```bash
git clone <this repo> nyx
cd nyx
sudo bash scripts/setup.sh
```

Then **edit `config.yaml`** — at minimum set:
- `network.lan_interface` — the interface your client devices connect through
- `network.wan_interface` — the interface facing the internet
- `network.lan_subnet` — your client-device subnet
- `vpn.config_path` — path to your WireGuard config

Edit `/etc/redsocks.conf` with your upstream proxy details if you plan to
use proxy mode.

## Running

```bash
sudo ./venv/bin/python3 main.py
```

Or via systemd (after `setup.sh`):
```bash
sudo systemctl enable --now nyx
```

## Using it

**CLI:**
```bash
python3 cli.py --status
python3 cli.py --mode vpn
python3 cli.py --mode tor
python3 cli.py --rotate            # manually trigger VPN rotation now
python3 cli.py --emergency-stop    # full open, all tunnels down (asks to confirm)
```

**Dashboard:**
```bash
# on your laptop:
ssh -L 5000:localhost:5000 pi@<pi-ip>
# then open http://localhost:5000
```
The dashboard has no built-in authentication — it's bound to `127.0.0.1`
on the Pi for that reason. Reach it via SSH tunnel, don't expose it on
your LAN or the internet as-is.

## Verification checklist (run these on the actual Pi — I can't test real
iptables/wg-quick/systemd behavior in this sandbox, only syntax and logic)

1. **Kill switch actually blocks forwarding:**
   ```bash
   sudo python3 cli.py mode vpn
   sudo wg-quick down wg0          # simulate VPN drop
   # from a CLIENT device (not the Pi), try to reach the internet — it should fail
   ping 8.8.8.8
   ```
   If that ping succeeds, the FORWARD rules aren't doing their job — check
   `sudo iptables -S FORWARD`.

2. **DNS doesn't leak:**
   From a client device, while in `tor` mode:
   ```bash
   nslookup example.com
   ```
   Check `sudo iptables -t nat -S PREROUTING` to confirm port 53 is being
   redirected to `tor.dns_port` from `config.yaml`.

3. **You don't lock yourself out:**
   Test the kill switch while SSH'd in from a LAN client, not from the
   Pi's own console — confirm your SSH session survives `enable_kill_switch()`.

4. **IPv6 isn't leaking:**
   ```bash
   curl -6 https://ifconfig.co  # should fail/timeout if block_ipv6: true
   ```

5. **Tor's own connection isn't routed through itself:**
   Confirm the `owner --uid-owner debian-tor` rule matches the actual user
   Tor runs as on your Pi OS version (`ps aux | grep tor`) — this varies
   slightly across Debian/Raspbian releases. Same check applies to the
   `redsocks` uid in the always-on proxy chain.

6. **VPN rotation actually rotates:**
   Set `vpn.rotation.interval_seconds: 20` temporarily, watch
   `logs/system.log` for "Rotating VPN: profile1 -> profile2", and confirm
   `python3 cli.py status` reflects the new profile each time.

7. **Watchdog detects a real drop:**
   While in `vpn` mode, manually kill the WireGuard interface
   (`sudo wg-quick down wg0`) from the Pi console (not through the CLI) and
   watch `logs/system.log` for the `CRITICAL` watchdog message within
   `firewall.watchdog_interval_seconds`. Confirm client devices lose
   connectivity immediately (fail-closed) rather than falling back to raw internet.

## Response to the detailed critical review (round 3)

Going point by point, since some of this was already shipped and some was a genuine gap:

| # | Item | Status |
|---|---|---|
| 1 | Routing forces ALL traffic through proxy/VPN/Tor | Already true (`firewall.py`: FORWARD default-DROP + mode-specific ACCEPT; OUTPUT REDIRECT to redsocks:12345 — literally the rule you cited) |
| 2 | DNS leak protection | Partially true before — client DNS was handled, but **the Pi's own DNS queries had no rule at all**, meaning they'd just fail under default-deny (broken, not leaking). Added `_apply_self_dns_protection()` — fixed in this round |
| 3 | IPv6 leak | ip6tables policy DROP existed; added the sysctl-level disable you suggested as a second, independent layer (`routing.disable_ipv6_stack()` + persisted in `scripts/setup.sh`) |
| 4 | Rotation risk if switch fails | Added `vpn_manager.preflight_check()` — validates config file + DNS-resolves the Endpoint host **before** touching the working tunnel. If preflight fails, rotation is skipped, zero disruption. Rollback-on-failure (round 2) remains the backstop for the case preflight passes but the handshake still fails (which preflight can't fully rule out without bringing the interface up — documented honestly in the code) |
| 5 | Proxy is TCP-only, document it | Already in README (round 2) — restating the exact phrasing here: **"Proxy layer uses redsocks (TCP only). UDP traffic may not be proxied."** Worth being precise though: this fails closed, not open — see "On redsocks/UDP" above, unmatched UDP is dropped, not leaked |
| 6 | Dashboard not fully integrated | Already true since round 2 (`/api/status`, working mode/rotate/emergency-stop). Added a live logs panel (`/api/logs`) this round |
| — | Thread safety | Real gap — `core/scheduler.py` and `core/watchdog.py` are separate threads both calling into the controller. Added `threading.RLock` around all mode-changing methods in `Controller`. Verified with a concurrency test (4 threads hammering `status()`/`check_tunnel_health()` during a mode switch, no exceptions) |
| — | Logging consistency | Already consistent — every module uses `logging.getLogger("nyx.system")`, configured once in `logger/logger.py` |
| — | subprocess return codes | Already checked everywhere via `firewall._run()` / each manager's `subprocess.run(...); if result.returncode != 0` pattern |
| — | `--mode` style CLI | `cli.py` rewritten with `argparse`: `python3 cli.py --mode vpn`, `--status`, `--rotate`, `--emergency-stop` |
| — | Full kill switch by default | Confirmed and made explicit: `main.py` calls `firewall.lockdown_only()` at boot **before** any mode is selected, so the default state (no mode active) is "block everything," not "allow everything." Surfaced as `traffic_blocked_by_default` in `/api/status` |

- **Not audited for production/adversarial use.** This is a personal
  hobby-project-grade privacy router, not a hardened appliance — treat it
  accordingly if your threat model is serious.
- **redsocks doesn't proxy UDP** by default, so proxy mode's DNS handling
  needs a deliberate choice (route DNS through Tor's DNSPort alongside
  redsocks, or point clients at a resolver reachable via the upstream
  proxy) — the code has a note where this decision needs to be made.
- **VPN-mode DNS** relies on client devices being configured (via your
  DHCP server) to use a resolver only reachable through the tunnel; it
  isn't forcibly redirected the way Tor/proxy mode's DNS is. If you want
  it forcibly redirected too, add a NAT rule pointing port 53 at your
  VPN's DNS server, mirroring the Tor-mode pattern in `firewall.py`.
- **No dashboard authentication** — see the note above.
- **Scheduler/rotation logic** (rotating VPN servers, auto-failover) from
  the original architecture diagram isn't built yet — this version is the
  correctness-first foundation to add that on top of.

## Project structure

```
nyx/
├── main.py              # daemon: owns Controller, IPC socket, monitor loop
├── cli.py                # CLI client (talks to daemon over Unix socket)
├── config.yaml
├── core/
│   ├── config_loader.py
│   ├── controller.py     # mode-switch ordering & fail-closed logic (vpn|tor only)
│   ├── scheduler.py      # VPN rotation thread
│   └── watchdog.py       # kill-switch watchdog thread
├── network/
│   ├── vpn_manager.py    # multi-profile support for rotation
│   ├── tor_manager.py
│   ├── proxy_manager.py  # always-on, not part of mode switching
│   ├── firewall.py       # kill switch, always-on proxy chain, DNS leak protection, NAT rules
│   └── routing.py        # IP forwarding
├── monitor/
│   ├── ip_monitor.py
│   └── bandwidth.py
├── logger/
│   └── logger.py
├── dashboard/
│   ├── app.py             # Flask, talks to daemon over Unix socket
│   └── templates/index.html
├── scripts/
│   ├── setup.sh
│   ├── redsocks.conf.template
│   ├── torrc.additions
│   └── nyx.service
└── logs/
```
