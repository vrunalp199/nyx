"""
Public IP + geolocation lookup. Fixes vs original:
  - bare `except:` replaced with specific exception handling + logging
  - http:// ip-api.com replaced with an HTTPS-capable API (leaking your own
    IP-check request in cleartext was a real, if minor, irony for a
    privacy tool)
  - timeouts added (original had none -> could hang forever on a stalled
    connection instead of failing fast)
"""

import requests
import logging

logger = logging.getLogger("nyx.system")

REQUEST_TIMEOUT = 5  # seconds


def get_ip_info(cfg) -> dict:
    ip_url = cfg["monitor"]["ip_check_url"]
    geo_url = cfg["monitor"]["geo_check_url"]

    try:
        ip = requests.get(ip_url, timeout=REQUEST_TIMEOUT).text.strip()
    except requests.RequestException as e:
        logger.warning(f"IP check failed: {e}")
        return {"ip": "unavailable", "error": str(e)}

    try:
        geo = requests.get(f"{geo_url}/{ip}/json/", timeout=REQUEST_TIMEOUT).json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Geo lookup failed: {e}")
        return {"ip": ip, "country": None, "city": None, "isp": None, "geo_error": str(e)}

    return {
        "ip": ip,
        "country": geo.get("country_name"),
        "city": geo.get("city"),
        "isp": geo.get("org"),
    }
