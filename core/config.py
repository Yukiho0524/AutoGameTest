"""Local machine configuration helpers.

Values can come from environment variables or config/local.json. The local file
is intentionally ignored by git so each computer can keep its own paths.
"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
LOCAL_CONFIG = os.path.join(CONFIG_DIR, "local.json")

_cache: dict | None = None
_status: dict = {
    "path": LOCAL_CONFIG,
    "exists": False,
    "format": "missing",
    "loaded": False,
    "error": "",
    "keys": [],
}


def _set_status(exists: bool, fmt: str, loaded: bool,
                error: str = "", keys: list[str] | None = None) -> None:
    global _status
    _status = {
        "path": LOCAL_CONFIG,
        "exists": exists,
        "format": fmt,
        "loaded": loaded,
        "error": error,
        "keys": sorted(keys or []),
    }


def _decode_lenient_string(value: str) -> str:
    result = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in ('"', "\\", "/"):
                result.append(nxt)
            else:
                result.append(ch + nxt)
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _parse_lenient_pairs(text: str) -> dict:
    pairs: dict[str, str] = {}
    pattern = re.compile(r'"([A-Za-z0-9_]+)"\s*:\s*"((?:\\.|[^"\\])*)"')
    for key, value in pattern.findall(text):
        pairs[key] = _decode_lenient_string(value).strip()
    return pairs


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.isfile(LOCAL_CONFIG):
        _cache = {}
        _set_status(False, "missing", False)
        return _cache
    try:
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
            text = f.read().lstrip("\ufeff")
    except OSError as e:
        _cache = {}
        _set_status(True, "error", False, str(e))
        return _cache
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        data = _parse_lenient_pairs(text)
        if data:
            _cache = data
            _set_status(
                True,
                "lenient",
                True,
                f"{e.msg} (line {e.lineno}, column {e.colno}); used key/value fallback",
                list(data.keys()),
            )
            return _cache
        _cache = {}
        _set_status(True, "error", False, f"{e.msg} (line {e.lineno}, column {e.colno})")
        return _cache
    if isinstance(data, dict):
        _cache = data
        _set_status(True, "json", True, keys=list(data.keys()))
    else:
        _cache = {}
        _set_status(True, "error", False, "local.json root must be an object")
    return _cache


def reload() -> dict:
    global _cache
    _cache = None
    return load()


def status() -> dict:
    load()
    return dict(_status)


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
