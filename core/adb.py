"""Thin wrapper around LDPlayer's ldconsole + bundled adb.

This is the control backend for `control == "emulator"` games. Every call here
drives the emulator through ADB, which does NOT touch the host mouse/keyboard --
that is the whole point of the A-plan (user can work while the AI plays).
"""
from __future__ import annotations

import os
import subprocess

from . import config

LDPLAYER_DIR = config.get("ldplayer_dir", "AUTOGAMETEST_LDPLAYER_DIR",
                          r"C:\LDPlayer\LDPlayer9")
LDCONSOLE = config.get("ldconsole_path", "AUTOGAMETEST_LDCONSOLE_PATH",
                       os.path.join(LDPLAYER_DIR, "ldconsole.exe"))
ADB = config.get("adb_path", "AUTOGAMETEST_ADB_PATH",
                 os.path.join(LDPLAYER_DIR, "adb.exe"))

_CREATE_NO_WINDOW = 0x08000000  # avoid popping a console window on Windows


def _run(args: list[str], timeout: int = 30, binary: bool = False):
    """Run a command, return (returncode, stdout, stderr). stdout is bytes if binary."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as e:
        return 127, b"" if binary else "", str(e)
    except subprocess.TimeoutExpired:
        return 124, b"" if binary else "", "timeout"
    out = proc.stdout if binary else proc.stdout.decode("utf-8", "ignore")
    err = proc.stderr.decode("utf-8", "ignore")
    return proc.returncode, out, err


def available() -> bool:
    return os.path.isfile(LDCONSOLE) and os.path.isfile(ADB)


def list_instances() -> list[dict]:
    """Parse `ldconsole list2` into structured rows.

    Columns: index,title,top_wnd,bind_wnd,running(0/1),pid,vbox_pid,w,h,dpi
    """
    rc, out, _ = _run([LDCONSOLE, "list2"])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 5:
            continue
        rows.append({
            "index": int(parts[0]),
            "title": parts[1],
            "running": parts[4] == "1",
            "width": int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else None,
            "height": int(parts[8]) if len(parts) > 8 and parts[8].isdigit() else None,
        })
    return rows


def launch_instance(index: int = 0) -> None:
    _run([LDCONSOLE, "launch", "--index", str(index)], timeout=15)


def serial_for(index: int = 0) -> str:
    """LDPlayer instance N maps to adb serial emulator-<5554 + N*2>."""
    return f"emulator-{5554 + index * 2}"


def adb_ready(serial: str) -> bool:
    rc, out, _ = _run([ADB, "-s", serial, "shell", "getprop", "sys.boot_completed"])
    return rc == 0 and out.strip() == "1"


def screenshot(serial: str) -> bytes | None:
    """Capture a PNG frame. Screencap-to-device then pull avoids the pipe
    corruption that stdout redirection causes on Windows."""
    dev = "/sdcard/_agt_cap.png"
    rc, _, _ = _run([ADB, "-s", serial, "shell", "screencap", "-p", dev])
    if rc != 0:
        return None
    rc, data, _ = _run([ADB, "-s", serial, "exec-out", "cat", dev], binary=True)
    if rc != 0 or not data:
        return None
    return data


def tap(serial: str, x: int, y: int) -> bool:
    rc, _, _ = _run([ADB, "-s", serial, "shell", "input", "tap", str(x), str(y)])
    return rc == 0


def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> bool:
    rc, _, _ = _run([ADB, "-s", serial, "shell", "input", "swipe",
                     str(x1), str(y1), str(x2), str(y2), str(ms)])
    return rc == 0


def launch_app(serial: str, package: str) -> bool:
    rc, _, _ = _run([ADB, "-s", serial, "shell", "monkey", "-p", package,
                     "-c", "android.intent.category.LAUNCHER", "1"])
    return rc == 0


def stop_app(serial: str, package: str) -> bool:
    rc, _, _ = _run([ADB, "-s", serial, "shell", "am", "force-stop", package])
    return rc == 0


def list_packages(serial: str, user_only: bool = True) -> list[str]:
    args = [ADB, "-s", serial, "shell", "pm", "list", "packages"]
    if user_only:
        args.append("-3")
    rc, out, _ = _run(args)
    if rc != 0:
        return []
    return sorted(l.replace("package:", "").strip() for l in out.splitlines() if l.strip())
