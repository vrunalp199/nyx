#!/bin/bash
# One-time setup on the Raspberry Pi. Run with: sudo bash scripts/setup.sh
set -e

echo "[+] Installing system dependencies..."
apt update
apt install -y python3-pip python3-venv tor wireguard iptables redsocks

echo "[+] Creating Python virtualenv..."
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

echo "[+] Copying redsocks config template..."
cp scripts/redsocks.conf.template /etc/redsocks.conf
echo "    -> Edit /etc/redsocks.conf with your upstream proxy details."

echo "[+] Copying Tor config additions..."
cat scripts/torrc.additions >> /etc/tor/torrc
echo "    -> Appended TransPort/DNSPort settings to /etc/tor/torrc"

echo "[+] Disabling IPv6 persistently (belt-and-suspenders with runtime sysctl + ip6tables)..."
if ! grep -q "disable_ipv6" /etc/sysctl.conf 2>/dev/null; then
    echo "net.ipv6.conf.all.disable_ipv6=1" >> /etc/sysctl.conf
    echo "net.ipv6.conf.default.disable_ipv6=1" >> /etc/sysctl.conf
    sysctl -p
fi

echo "[+] Installing systemd service..."
cp scripts/nyx.service /etc/systemd/system/
systemctl daemon-reload
echo "    -> Enable with: sudo systemctl enable nyx"
echo "    -> Start with:  sudo systemctl start nyx"

echo "[+] Done. Review config.yaml before starting -- especially"
echo "    network.lan_interface / wan_interface / lan_subnet."
