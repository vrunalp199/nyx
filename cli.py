"""
CLI client. Talks to the running daemon over the Unix socket.

Usage:
    python3 cli.py --mode vpn
    python3 cli.py --mode tor
    python3 cli.py --status
    python3 cli.py --rotate
    python3 cli.py --emergency-stop
"""

import argparse
import json
import socket
import sys

SOCKET_PATH = "/tmp/nyx.sock"


def send_request(request: dict) -> dict:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        print("Error: daemon not running. Start it first with: sudo python3 main.py")
        sys.exit(1)

    client.sendall(json.dumps(request).encode())
    response = client.recv(65536)
    client.close()
    return json.loads(response.decode())


def print_result(resp):
    if resp.get("ok"):
        print(json.dumps(resp["result"], indent=2))
    else:
        print(f"Error: {resp.get('error')}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Pi Privacy Router CLI")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=["vpn", "tor"], help="switch mode")
    group.add_argument("--status", action="store_true", help="show current status")
    group.add_argument("--rotate", action="store_true", help="trigger VPN rotation now")
    group.add_argument("--emergency-stop", action="store_true",
                        help="disable kill switch, stop all tunnels (asks to confirm)")
    args = parser.parse_args()

    if args.mode:
        print_result(send_request({"action": "set_mode", "mode": args.mode}))
    elif args.status:
        print_result(send_request({"action": "status"}))
    elif args.rotate:
        print_result(send_request({"action": "rotate_vpn"}))
    elif args.emergency_stop:
        confirm = input("This disables the kill switch and stops all tunnels. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Cancelled.")
            sys.exit(0)
        print_result(send_request({"action": "emergency_stop"}))


if __name__ == "__main__":
    main()
