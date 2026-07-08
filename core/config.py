"""Local machine configuration helpers.

Values can come from environment variables or config/local.json. The local file
is intentionally ignored by git so each computer can keep its own paths.
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
LOCAL_CONFIG = os.path.join(CONFIG_DIR, "local.json")

_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.isfile(LOCAL_CONFIG):
        _cache = {}
        return _cache
    try:
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    _cache = data if isinstance(data, dict) else {}
    return _cache


def get(name: str, env: str | None = None, default: str = "") -> str:
    if env:
        value = os.environ.get(env, "").strip()
        if value:
            return os.path.expandvars(os.path.expanduser(value))
    value = str(load().get(name, "") or "").strip()
    if value:
        return os.path.expandvars(os.path.expanduser(value))
    return default

