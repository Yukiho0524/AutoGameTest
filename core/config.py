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


def reload() -> dict:
    global _cache
    _cache = None
    return load()


def _clean(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.expandvars(os.path.expanduser(text))


def get_any(names: list[str], envs: list[str] | None = None,
            default: str = "") -> str:
    """Read the first configured value from env vars or local.json aliases."""
    for env in envs or []:
        value = _clean(os.environ.get(env, ""))
        if value:
            return value
    local = load()
    for name in names:
        value = _clean(local.get(name, ""))
        if value:
            return value
    return default


def get(name: str, env: str | None = None, default: str = "") -> str:
    return get_any([name], [env] if env else None, default)
