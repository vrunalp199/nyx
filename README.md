<div align="center">

# Nyx

<img src="assets/banner.png" alt="Nyx — privacy-first router for Raspberry Pi" width="100%">

![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-a4133c?style=for-the-badge) ![Focus](https://img.shields.io/badge/Focus-Privacy%20%26%20Networking-darkred?style=for-the-badge) ![Python](https://img.shields.io/badge/Python-3.9+-blue?style=for-the-badge) ![Firewall](https://img.shields.io/badge/Firewall-iptables-2dd4bf?style=for-the-badge) ![Modes](https://img.shields.io/badge/Modes-VPN%20%7C%20Tor-gold?style=for-the-badge) ![Status](https://img.shields.io/badge/Status-Hobby%20Project-fb923c?style=for-the-badge) ![License](https://img.shields.io/badge/License-MIT-lightgrey?style=for-the-badge)

**A Raspberry Pi that sits between your devices and the internet, forcing all traffic through a VPN, Tor, or a proxy — with a real kill switch, DNS leak protection, and a CLI/dashboard to control it.**

</div>

---

# 📖 Overview

Nyx turns a Raspberry Pi into a fail-closed privacy gateway. Every device on your LAN routes through it, and the Pi enforces one of two switchable modes — **VPN** or **Tor** — while an always-on proxy layer handles the Pi's own outbound traffic.

The project combines:

- Fail-closed `iptables` kill switch (`FORWARD`/`OUTPUT` default `DROP`)
- Switchable **VPN** (WireGuard) / **Tor** client-traffic modes
- Always-on upstream proxy layer for the Pi's own traffic (`redsocks`)
- DNS and IPv6 leak protection at both the firewall and kernel level
- VPN profile rotation with preflight checks and rollback
- Kill-switch watchdog for tunnel-drop detection and recovery
- CLI + local dashboard, both talking to one daemon over a Unix socket

> Personal hobby project, not audited for production/adversarial use. If your threat model is serious, treat this as a starting point, not a hardened appliance.

---

# ✨ Features

## Traffic Control

- Fail-closed kill switch — nothing routes until a mode is actively applied
- Switchable modes: **VPN** or **Tor** (client-device traffic)
- Always-on proxy layer for the Pi's own traffic, independent of client mode
- VPN profile rotation on a timer, with preflight checks and automatic rollback on failed rotation
- Emergency stop (full open, all tunnels down — confirmation required)

## Leak Protection

- DNS leak protection — explicitly redirected (Tor) or scoped to the tunnel interface (VPN), for clients *and* the Pi itself
- IPv6 leak protection — blocked at both `ip6tables` policy and kernel `sysctl` level
- Kill-switch watchdog — detects a dropped tunnel, logs `CRITICAL`, attempts reconnect

## Control

- CLI (`cli.py --status / --mode / --rotate / --emergency-stop`)
- Local dashboard (Flask, SSH-tunnel only, no built-in auth)
- Single daemon owns firewall state — CLI and dashboard can't fight over `iptables`

---

# 🏗 System Architecture

```text
        ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
        │ Phone (Wi-Fi) │   │Laptop (Wi-Fi) │   │ Other Device  │
        └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
                │                   │                   │
                └───────────────────┼───────────────────┘
                                    ▼
                        ┌─────────────────────────┐
                        │      Raspberry Pi       │
                        │   (Nyx daemon + Wi-Fi)  │
                        └────────────┬────────────┘
                                     │
                                     ▼
                        ┌────────────────────────┐
                        │Proxy Chain (always on) │
                        │       redsocks         │
                        └────────────┬───────────┘
                                     │
                                     ▼
                        ┌────────────────────────┐
                        │ Selected Mode (one)    │
                        │                        │
                        │ ○ VPN                  │
                        │ ○ Tor                  │
                        │ ○ VPN (rotating)       │
                        └────────────┬───────────┘
                                     │
                                     ▼
                              ┌────────────┐
                              │  Internet  │
                              └────────────┘
```

**Why proxy isn't a third mode:** a single packet can't be NAT-redirected to two destinations (proxy *and* VPN/Tor) with plain `iptables REDIRECT`. So the proxy layer governs the Pi's own outbound traffic (`OUTPUT` chain) and runs always-on, while VPN/Tor remain the switchable modes governing client traffic (`FORWARD` chain + NAT `PREROUTING`). Full reasoning in [Architecture Notes](#-architecture-notes) below.

---

# 🛠 Technology Stack

## Networking

| Component | Technology |
|------------|------------|
| Firewall / kill switch | iptables / ip6tables |
| VPN | WireGuard (`wg-quick`) |
| Anonymity network | Tor |
| Upstream proxy | redsocks (TCP only) |
| IPv6 protection | `sysctl` + `ip6tables` |

## Backend

| Component | Technology |
|------------|------------|
| Language | Python 3.9+ |
| Daemon ↔ client IPC | Unix domain socket |
| Dashboard | Flask |
| System metrics | psutil |
| Process management | systemd |

---

# 🔍 Mode-Switch Flow

```text
CLI / Dashboard Request
          │
          ▼
   Controller.set_mode()
          │
          ▼
   firewall.lockdown_only()
          │
          ▼
   Stop old service / Start new service
          │
          ▼
   apply_routing_rules()
          │
          ▼
   enable_proxy_chain()
          │
          ▼
      Mode Active
```

Lockdown is applied *before* the new service starts, so there's no window where traffic could slip out unprotected between modes — the whole path fails closed by default.

---

# 📂 Project Structure

```text
nyx/
│
├── main.py               # daemon: owns Controller, IPC socket, monitor loop
├── cli.py                 # CLI client (talks to daemon over Unix socket)
├── config.yaml
├── requirements.txt
├── README.md
├── CHANGELOG.md
│
├── core/
│   ├── config_loader.py
│   ├── controller.py      # mode-switch ordering & fail-closed logic (vpn|tor only)
│   ├── scheduler.py       # VPN rotation thread
│   └── watchdog.py        # kill-switch watchdog thread
│
├── network/
│   ├── vpn_manager.py     # multi-profile support for rotation
│   ├── tor_manager.py
│   ├── proxy_manager.py   # always-on, not part of mode switching
│   ├── firewall.py        # kill switch, proxy chain, DNS leak protection, NAT rules
│   └── routing.py         # IP forwarding
│
├── monitor/
│   ├── ip_monitor.py
│   ├── bandwidth.py
│   └── resource_monitor.py
│
├── logger/
│   └── logger.py
│
├── dashboard/
│   ├── app.py              # Flask, talks to daemon over Unix socket
│   └── templates/index.html
│
├── scripts/
│   ├── setup.sh
│   ├── redsocks.conf.template
│   ├── torrc.additions
│   └── nyx.service
│
└── logs/
```

---

# ⚙ Installation

```bash
git clone https://github.com/vrunalp199/nyx
cd nyx
sudo bash scripts/setup.sh
```

Then edit `config.yaml` — at minimum set:

| Key | Meaning |
|---|---|
| `network.lan_interface` | Interface your client devices connect through |
| `network.wan_interface` | Interface facing the internet |
| `network.lan_subnet` | Your client-device subnet |
| `vpn.config_path` | Path to your WireGuard config |

If you plan to use proxy mode, also edit `/etc/redsocks.conf` with your upstream proxy details.

**Requirements:** a Raspberry Pi with two network paths (e.g. Ethernet in, Wi-Fi out to clients — or vice versa), Raspberry Pi OS (Debian-based), and root access (the daemon manages `iptables`/`wg-quick`/`systemd`).

---

# ▶ Running

```bash
sudo ./venv/bin/python3 main.py
```

Or via systemd (after `setup.sh`):

```bash
sudo systemctl enable --now nyx
```

---

# 🖥 Usage

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

The dashboard has **no built-in authentication** — it's bound to `127.0.0.1` on the Pi for that reason. Reach it via SSH tunnel; don't expose it on your LAN or the internet as-is.

---

# 🧪 Verification Checklist

These need a real Pi — `iptables`/`wg-quick`/`systemd` behavior isn't testable in a dev sandbox, only syntax and logic.

1. **Kill switch blocks forwarding on VPN drop**
   ```bash
   sudo python3 cli.py --mode vpn
   sudo wg-quick down wg0          # simulate VPN drop
   ping 8.8.8.8                    # from a CLIENT device — should fail
   ```
2. **DNS doesn't leak** (`tor` mode) — `nslookup example.com` from a client; confirm port 53 redirect via `sudo iptables -t nat -S PREROUTING`.
3. **No SSH lockout** — test the kill switch from an SSH session on a LAN client, confirm it survives `enable_kill_switch()`.
4. **IPv6 isn't leaking** — `curl -6 https://ifconfig.co` should fail/timeout if `block_ipv6: true`.
5. **Tor isn't routed through itself** — confirm `owner --uid-owner debian-tor` matches the real Tor user on your OS (`ps aux | grep tor`); same check for `redsocks`.
6. **VPN rotation actually rotates** — set `vpn.rotation.interval_seconds: 20`, watch `logs/system.log` for the rotation message, confirm `cli.py --status` reflects it.
7. **Watchdog detects a real drop** — kill WireGuard from the Pi console directly, watch for a `CRITICAL` watchdog message within `firewall.watchdog_interval_seconds`.

---

# 📝 Architecture Notes

**redsocks and UDP:** redsocks only proxies TCP. This fails closed, not open — default `OUTPUT`/`FORWARD` policy is `DROP`, and unmatched UDP has nowhere to go, so it's dropped rather than sent unprotected. In VPN mode client UDP goes through the tunnel fine; in Tor mode, non-DNS UDP (VoIP, games, QUIC/HTTP3) is simply blocked.

**Dashboard data:** short-poll (2s interval), not push-based. Reads real data from `Controller.status()` — actual bandwidth from consecutive `psutil` snapshots, actual rotation countdown, actual CPU/mem. A push-based view (SocketIO/SSE) would be a future upgrade.

---

# ⚠ Known Limitations

- Not audited for production/adversarial use.
- redsocks doesn't proxy UDP — proxy mode's DNS handling needs a deliberate choice (route via Tor's DNSPort, or a resolver reachable through the upstream proxy).
- VPN-mode DNS relies on client DHCP config pointing at a resolver reachable only through the tunnel — it isn't forcibly redirected the way Tor/proxy mode is.
- No dashboard authentication — SSH-tunnel only.

---

# 🛣 Roadmap

## Phase 1
- Fail-closed kill switch, mode switching, DNS/IPv6 leak protection

## Phase 2
- VPN rotation, watchdog, dashboard wired to live backend, thread-safe controller

## Phase 3
- Client-traffic double-wrapping (proxy → VPN)
- Push-based dashboard (SocketIO/SSE)
- Dashboard authentication

## Phase 4
- Multi-Pi / mesh configuration
- Packaged installer / image

---

# ⚠ Disclaimer

Nyx is intended for personal privacy and educational use. It is a hobby project, not a hardened or audited security appliance — evaluate it accordingly if your threat model is serious. Users are responsible for complying with all applicable laws and regulations, including local laws around VPN/Tor use.

---

# 📜 License

MIT License

---

## 👨‍💻 Author

<p align="center">
  <b>Vrunal Patil</b><br>
  ⭐ If you find this project useful, consider starring the repository
</p>
