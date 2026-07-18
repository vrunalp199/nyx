"""Bandwidth counters via psutil (unchanged logic from prototype, this
part was already correct -- just typed/documented)."""

import psutil


def get_bandwidth() -> dict:
    net = psutil.net_io_counters()
    return {
        "sent_bytes": net.bytes_sent,
        "recv_bytes": net.bytes_recv,
    }
