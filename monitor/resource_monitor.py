"""
Resource monitoring (Raspberry Pi hardware can genuinely get overloaded
running Tor + VPN + redsocks + polling at once). Not a hard throttle in
this version -- just visibility: logs a WARNING when usage crosses the
configured threshold, and surfaces current CPU/mem to the dashboard.

Config:
    system:
      max_cpu_percent: 80
      max_mem_percent: 85
"""

import psutil
import logging

logger = logging.getLogger("nyx.system")


def check_resources(cfg) -> dict:
    system_cfg = cfg.get("system", {})
    max_cpu = system_cfg.get("max_cpu_percent", 90)
    max_mem = system_cfg.get("max_mem_percent", 90)

    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory().percent

    warnings = []
    if cpu > max_cpu:
        warnings.append(f"CPU usage {cpu:.0f}% exceeds limit {max_cpu}%")
        logger.warning(f"High CPU usage: {cpu:.0f}% (limit {max_cpu}%)")
    if mem > max_mem:
        warnings.append(f"Memory usage {mem:.0f}% exceeds limit {max_mem}%")
        logger.warning(f"High memory usage: {mem:.0f}% (limit {max_mem}%)")

    return {
        "cpu_percent": cpu,
        "mem_percent": mem,
        "max_cpu_percent": max_cpu,
        "max_mem_percent": max_mem,
        "warnings": warnings,
    }
