"""Platform detection from a game executable path.

The launch method differs per platform, so the UI needs to know which platform a
game belongs to. Rather than make the user pick blindly, we infer the platform
from the exe path and let them confirm/override.
"""
from __future__ import annotations

import os
import re
import glob

# Ordered: first matching rule wins. Keys are lowercase substrings of the path.
_PATH_RULES = [
    ("steamapps\\common", "steam"),
    ("steamapps/common", "steam"),
    ("epic games\\", "epic"),
    ("epic games/", "epic"),
    ("windowsapps", "xbox"),
]

PLATFORMS = {
    "steam": {"label": "Steam", "control": "desktop"},
    "epic": {"label": "Epic Games", "control": "desktop"},
    "xbox": {"label": "Xbox / Microsoft Store", "control": "desktop"},
    "pc": {"label": "一般 PC 單機", "control": "desktop"},
    "android": {"label": "Android 模擬器", "control": "emulator"},
}


def detect_platform(exe_path: str) -> dict:
    """Infer platform + launch hints from an exe path.

    Returns a dict: {platform, control, label, hints{...}}.
    For Steam, hints include steam_appid when it can be read from appmanifest.
    """
    p = (exe_path or "").lower()
    platform = "pc"
    for needle, name in _PATH_RULES:
        if needle in p:
            platform = name
            break

    info = PLATFORMS[platform]
    result = {
        "platform": platform,
        "control": info["control"],
        "label": info["label"],
        "hints": {},
    }

    if platform == "steam":
        appid = _find_steam_appid(exe_path)
        if appid:
            result["hints"]["steam_appid"] = appid
    return result


def _find_steam_appid(exe_path: str) -> str | None:
    """Walk up from the exe to steamapps and read the matching appmanifest_*.acf.

    A game lives at ...\\steamapps\\common\\<GameFolder>\\...\\game.exe
    and its manifest is ...\\steamapps\\appmanifest_<appid>.acf whose
    "installdir" field equals <GameFolder>.
    """
    try:
        norm = exe_path.replace("/", os.sep)
        lower = norm.lower()
        marker = "steamapps" + os.sep + "common" + os.sep
        idx = lower.find(marker)
        if idx == -1:
            return None
        steamapps = norm[: idx + len("steamapps")]
        after = norm[idx + len(marker):]
        game_folder = after.split(os.sep)[0]

        for acf in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
            try:
                with open(acf, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            m = re.search(r'"installdir"\s*"([^"]+)"', text)
            if m and m.group(1).lower() == game_folder.lower():
                am = re.search(r'"appid"\s*"(\d+)"', text)
                if am:
                    return am.group(1)
        return None
    except Exception:
        return None
