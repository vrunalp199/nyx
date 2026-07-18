"""
Loads and validates config.yaml. Every other module pulls settings
from here instead of hardcoding values (this fixes the original bug
where vpn_manager.py hardcoded 'wg0' and main.py ignored config.yaml
entirely).
"""

import yaml
import os

_CONFIG_CACHE = None


class ConfigError(Exception):
    pass


def load_config(path="config.yaml"):
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    required_top_level = ["network", "vpn", "tor", "proxy", "firewall", "monitor", "logging"]
    for key in required_top_level:
        if key not in cfg:
            raise ConfigError(f"Missing required config section: '{key}'")

    _CONFIG_CACHE = cfg
    return cfg


def reload_config(path="config.yaml"):
    """Force re-read from disk (e.g. dashboard 'reload config' button)."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    return load_config(path)
