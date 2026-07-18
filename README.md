# Nyx

A Raspberry Pi that sits between your devices and the internet, forcing all
traffic through a VPN, Tor, or a proxy — with a real kill switch, DNS leak
protection, and a CLI/dashboard to control it.

> Personal hobby project, not audited for production/adversarial use. If
> your threat model is serious, treat this as a starting point, not a
> hardened appliance.

## Features

- **Fail-closed kill switch** — `FORWARD`/`OUTPUT` default to `DROP`; traffic
  only flows once a mode is actively applied.
- **Switchable modes: VPN or Tor** — govern client-device traffic (`FORWARD`
  chain + NAT `PREROUTING`).
- **Always-on proxy layer** — routes the Pi's *own* outbound traffic
  (updates, IP checks) through an upstream proxy via redsocks, independent
  of the client-facing mode. See [Architecture](#architecture) for why this
  isn't a third "mode."
- **DNS leak protection** — DNS is explicitly redirected (Tor) or scoped to
  the tunnel interface (VPN), for both client devices and the Pi itself.
- **IPv6 leak protection** — blocked at both the `ip6tables` policy level
  and the kernel (`sysctl`) level.
- **VPN rotation** — cycles through configured VPN profiles on an interval,
  with preflight checks and rollback if a rotation fails.
- **Kill-switch watchdog** — detects a dropped tunnel and logs/attempts
  recovery; the block itself is automatic (dead interface = no matching
  rule), the watchdog adds detection and reconnect.
- **CLI + dashboard**, both talking to a single daemon over a Unix socket
  so they can't fight over iptables state.

## Architecture

### Proxy is not a mode

A single packet can't be NAT-redirected to two destinations at once (proxy
*and* VPN, or proxy *and* Tor) with plain iptables `REDIRECT`. So:

- **Proxy (redsocks)** is an **always-on layer for the Pi's own outbound
  traffic** — lives in the `OUTPUT` chain, chained to your upstream proxy
  in `/etc/redsocks.conf`. Started at boot (`main.py`) and re-applied after
  every mode switch.
- **VPN / Tor** are the switchable modes and govern **client-device
  traffic** — `FORWARD` chain + NAT `PREROUTING`.

If you want client traffic double-wrapped (client → proxy → VPN, so your
ISP sees a proxy connection instead of a raw WireGuard handshake), that's a
heavier build: redsocks' upstream target would need to *be* the VPN's SOCKS
capability, which WireGuard doesn't natively expose. Not implemented here.

### redsocks and UDP

redsocks only proxies TCP. This is **fail-closed, not a leak**: default
`OUTPUT`/`FORWARD` policy is `DROP`, and redsocks' `REDIRECT` rule only
matches `-p tcp`. Unmatched UDP has nowhere to go and is dropped, not sent
unprotected.

- **VPN mode:** client UDP is explicitly allowed via `FORWARD ACCEPT -o
  <vpn_if>`, so it goes through the tunnel fine.
- **Tor mode:** non-DNS UDP (VoIP, games, QUIC/HTTP3) has no matching rule
  and is simply blocked. Those app categories won't work in Tor mode —
  worth knowing going in.

### Dashboard data

The dashboard is short-poll (2s interval), not push-based. It reads real
data from `Controller.status()`: bandwidth rate from two consecutive
`psutil` snapshots, rotation countdown from `vpn.rotation.interval_seconds`,
and live CPU/mem. A true push-based view (Flask-SocketIO or SSE) would be a
future upgrade.

## Requirements

- Raspberry Pi with two network paths (e.g. Ethernet in from your router,
  Wi-Fi for client devices — or vice versa)
- Raspberry Pi OS (Debian-based)
- Root access (the daemon manages iptables/wg-quick/systemd)

## Setup

```bash
git clone <this repo> nyx
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

If you plan to use proxy mode, also edit `/etc/redsocks.conf` with your
upstream proxy details.

## Running

```bash
sudo ./venv/bin/python3 main.py
```

Or via systemd (after `setup.sh`):

```bash
sudo systemctl enable --now nyx
```

## Usage

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

The dashboard has **no built-in authentication** — it's bound to
`127.0.0.1` on the Pi for that reason. Reach it via SSH tunnel; don't
expose it on your LAN or the internet as-is.

## Verification checklist

These need a real Pi — iptables/wg-quick/systemd behavior isn't testable
in a dev sandbox, only syntax and logic.

1. **Kill switch blocks forwarding on VPN drop**
   ```bash
   sudo python3 cli.py --mode vpn
   sudo wg-quick down wg0          # simulate VPN drop
   # from a CLIENT device (not the Pi):
   ping 8.8.8.8                    # should fail
   ```
   If the ping succeeds, check `sudo iptables -S FORWARD`.

2. **DNS doesn't leak** — in `tor` mode, from a client device:
   ```bash
   nslookup example.com
   ```
   Confirm port 53 is redirected via `sudo iptables -t nat -S PREROUTING`
   (should match `tor.dns_port` from `config.yaml`).

3. **You don't lock yourself out** — test the kill switch from an SSH
   session on a LAN client (not the Pi's own console); confirm the session
   survives `enable_kill_switch()`.

4. **IPv6 isn't leaking:**
   ```bash
   curl -6 https://ifconfig.co   # should fail/timeout if block_ipv6: true
   ```

5. **Tor isn't routed through itself** — confirm the `owner
   --uid-owner debian-tor` rule matches the actual user Tor runs as on your
   OS version (`ps aux | grep tor`); same check for the `redsocks` uid.

6. **VPN rotation actually rotates** — set
   `vpn.rotation.interval_seconds: 20` temporarily, watch `logs/system.log`
   for `Rotating VPN: profile1 -> profile2`, confirm `cli.py --status`
   reflects the new profile each time.

7. **Watchdog detects a real drop** — in `vpn` mode, kill WireGuard from
   the Pi console directly (`sudo wg-quick down wg0`, not via the CLI) and
   watch for a `CRITICAL` watchdog message in `logs/system.log` within
   `firewall.watchdog_interval_seconds`. Client devices should lose
   connectivity immediately rather than fall back to raw internet.

## Known limitations

- **Not audited for production/adversarial use.**
- **redsocks doesn't proxy UDP.** Proxy mode's DNS handling needs a
  deliberate choice: route DNS through Tor's DNSPort alongside redsocks, or
  point clients at a resolver reachable via the upstream proxy.
- **VPN-mode DNS** relies on client devices being configured (via DHCP) to
  use a resolver reachable only through the tunnel — it isn't forcibly
  redirected the way Tor/proxy mode's DNS is. To force it, add a NAT rule
  pointing port 53 at your VPN's DNS server, mirroring the Tor-mode pattern
  in `firewall.py`.
- **No dashboard authentication** — SSH-tunnel only, see above.

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

## License

MIT
