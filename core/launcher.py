"""Launch a game by platform. Desktop games go through the OS/launcher,
emulator games go through ADB."""
from __future__ import annotations

import os
import subprocess

from . import adb

_CREATE_NO_WINDOW = 0x08000000


def launch(game: dict) -> dict:
    """Launch the given game record. Returns {ok, method, detail}."""
    platform = game.get("platform")
    lc = game.get("launch", {})

    if game.get("control") == "emulator":
        return _launch_emulator(lc)

    if platform == "steam":
        appid = lc.get("steam_appid")
        if appid:
            return _launch_uri(f"steam://rungameid/{appid}", "steam-protocol")
        return _launch_exe(lc.get("exe_path"))

    if platform == "epic":
        app = lc.get("epic_app_name")
        if app:
            uri = f"com.epicgames.launcher://apps/{app}?action=launch&silent=true"
            return _launch_uri(uri, "epic-protocol")
        return _launch_exe(lc.get("exe_path"))

    if platform == "xbox":
        aumid = lc.get("aumid")  # AppsFolder\<PFN>!<AppId>
        if aumid:
            return _launch_uri(f"shell:AppsFolder\\{aumid}", "shell-aumid")
        return _launch_exe(lc.get("exe_path"))

    # plain pc
    return _launch_exe(lc.get("exe_path"))


def _launch_uri(uri: str, method: str) -> dict:
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return {"ok": True, "method": method, "detail": uri}
    except Exception as e:
        return {"ok": False, "method": method, "detail": str(e)}


def _launch_exe(exe_path: str | None) -> dict:
    if not exe_path or not os.path.isfile(exe_path):
        return {"ok": False, "method": "exe", "detail": f"exe not found: {exe_path}"}
    try:
        subprocess.Popen([exe_path], creationflags=_CREATE_NO_WINDOW,
                         cwd=os.path.dirname(exe_path))
        return {"ok": True, "method": "exe", "detail": exe_path}
    except Exception as e:
        return {"ok": False, "method": "exe", "detail": str(e)}


def _launch_emulator(lc: dict) -> dict:
    if not adb.available():
        return {"ok": False, "method": "emulator", "detail": "LDPlayer/adb not found"}
    index = lc.get("instance", 0)
    serial = lc.get("serial") or adb.serial_for(index)
    package = lc.get("package")

    instances = {r["index"]: r for r in adb.list_instances()}
    if index not in instances or not instances[index]["running"]:
        adb.launch_instance(index)
        return {"ok": True, "method": "emulator",
                "detail": f"啟動模擬器實例 {index} 中，開機後請再按一次啟動遊戲"}

    if not package:
        return {"ok": False, "method": "emulator", "detail": "缺少 package"}
    if not adb.adb_ready(serial):
        return {"ok": False, "method": "emulator", "detail": f"{serial} 尚未就緒（開機中？）"}
    ok = adb.launch_app(serial, package)
    return {"ok": ok, "method": "emulator", "detail": f"{serial} -> {package}"}
