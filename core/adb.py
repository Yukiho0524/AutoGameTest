"""Thin wrappers around Android emulator ADB backends.

Supported emulator backends:
- LDPlayer 9: ldconsole.exe + adb.exe
- BlueStacks 5: HD-Player.exe + HD-Adb.exe

All control still goes through ADB, so emulator games do not touch the host
mouse/keyboard.
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

_PROGRAM_FILES = os.environ.get("ProgramFiles", r"C:\Program Files")


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen = set()
    result = []
    for path in paths:
        path = os.path.normpath(os.path.expandvars(os.path.expanduser(path or "")))
        if not path:
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _default_bluestacks_dirs() -> list[str]:
    dirs = [
        os.path.join(_PROGRAM_FILES, "BlueStacks_nxt"),
        os.path.join(_PROGRAM_FILES, "BlueStacks"),
    ]
    for drive in ("C:", "D:", "E:"):
        dirs.extend([
            os.path.join(drive + "\\", "BlueStacks_nxt"),
            os.path.join(drive + "\\", "BlueStacks"),
            os.path.join(drive + "\\", "Bluestacks_nxt"),
            os.path.join(drive + "\\", "Bluestacks"),
            os.path.join(drive + "\\", "Bluestack", "BlueStacks_nxt"),
            os.path.join(drive + "\\", "Bluestack", "BlueStacks"),
            os.path.join(drive + "\\", "Program Files", "BlueStacks_nxt"),
            os.path.join(drive + "\\", "Program Files", "BlueStacks"),
        ])
    return _dedupe_paths(dirs)


def _bluestacks_dir_candidates(configured_dir: str = "",
                               include_defaults: bool = True) -> list[str]:
    dirs = []
    if configured_dir:
        base = os.path.dirname(configured_dir) if os.path.isfile(configured_dir) else configured_dir
        dirs.extend([
            base,
            os.path.join(base, "BlueStacks_nxt"),
            os.path.join(base, "BlueStacks"),
            os.path.join(base, "Bluestacks_nxt"),
            os.path.join(base, "Bluestacks"),
        ])
    if include_defaults:
        dirs.extend(_default_bluestacks_dirs())
    return _dedupe_paths(dirs)


def _resolve_file(configured_path: str, dirs: list[str], filename: str) -> str:
    if configured_path:
        if os.path.isfile(configured_path):
            return configured_path
        if os.path.isdir(configured_path):
            candidate = os.path.join(configured_path, filename)
            if os.path.isfile(candidate):
                return candidate
        return configured_path
    for directory in dirs:
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(dirs[0], filename)


def _resolve_bluestacks_paths() -> tuple[str, str, str]:
    configured_dir = config.get_any(
        ["bluestacks_dir", "bluestack_dir", "bs_dir"],
        ["AUTOGAMETEST_BLUESTACKS_DIR", "AUTOGAMETEST_BLUESTACK_DIR"],
    )
    player_config = config.get_any(
        ["bluestacks_player_path", "bluestack_player_path",
         "bluestacks_player", "hd_player_path"],
        ["AUTOGAMETEST_BLUESTACKS_PLAYER_PATH",
         "AUTOGAMETEST_BLUESTACK_PLAYER_PATH"],
    )
    adb_config = config.get_any(
        ["bluestacks_adb_path", "bluestack_adb_path",
         "bluestacks_adb", "hd_adb_path"],
        ["AUTOGAMETEST_BLUESTACKS_ADB_PATH",
         "AUTOGAMETEST_BLUESTACK_ADB_PATH"],
    )
    dirs = _bluestacks_dir_candidates(
        configured_dir,
        include_defaults=not bool(configured_dir),
    )
    player_path = _resolve_file(
        player_config,
        dirs,
        "HD-Player.exe",
    )
    adb_path = _resolve_file(
        adb_config,
        dirs,
        "HD-Adb.exe",
    )
    if os.path.isfile(player_path):
        base_dir = os.path.dirname(player_path)
    elif os.path.isfile(adb_path):
        base_dir = os.path.dirname(adb_path)
    elif configured_dir:
        base_dir = configured_dir
    else:
        base_dir = dirs[0]
    return base_dir, player_path, adb_path


BLUESTACKS_DIR, BLUESTACKS_PLAYER, BLUESTACKS_ADB = _resolve_bluestacks_paths()
BLUESTACKS_SERIAL = config.get("bluestacks_serial", "AUTOGAMETEST_BLUESTACKS_SERIAL",
                               "127.0.0.1:5555")
BLUESTACKS_INSTANCE = config.get("bluestacks_instance",
                                 "AUTOGAMETEST_BLUESTACKS_INSTANCE", "")

_CREATE_NO_WINDOW = 0x08000000


def reload_config_paths() -> None:
    """Reload local config and recompute emulator executable paths."""
    global LDPLAYER_DIR, LDCONSOLE, ADB
    global BLUESTACKS_DIR, BLUESTACKS_PLAYER, BLUESTACKS_ADB
    global BLUESTACKS_SERIAL, BLUESTACKS_INSTANCE

    config.reload()
    LDPLAYER_DIR = config.get("ldplayer_dir", "AUTOGAMETEST_LDPLAYER_DIR",
                              r"C:\LDPlayer\LDPlayer9")
    LDCONSOLE = config.get("ldconsole_path", "AUTOGAMETEST_LDCONSOLE_PATH",
                           os.path.join(LDPLAYER_DIR, "ldconsole.exe"))
    ADB = config.get("adb_path", "AUTOGAMETEST_ADB_PATH",
                     os.path.join(LDPLAYER_DIR, "adb.exe"))
    BLUESTACKS_DIR, BLUESTACKS_PLAYER, BLUESTACKS_ADB = _resolve_bluestacks_paths()
    BLUESTACKS_SERIAL = config.get(
        "bluestacks_serial", "AUTOGAMETEST_BLUESTACKS_SERIAL", "127.0.0.1:5555")
    BLUESTACKS_INSTANCE = config.get(
        "bluestacks_instance", "AUTOGAMETEST_BLUESTACKS_INSTANCE", "")


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


def normalize_emulator(emulator: str | None = None) -> str:
    value = (emulator or "").strip().lower()
    if value in ("bluestacks", "bluestack", "bs", "bs5"):
        return "bluestacks"
    return "ldplayer"


def emulator_for_serial(serial: str | None) -> str:
    serial = (serial or "").lower()
    if serial.startswith("127.0.0.1:") or serial.startswith("localhost:"):
        return "bluestacks"
    return "ldplayer"


def adb_path_for(emulator: str | None = None) -> str:
    return BLUESTACKS_ADB if normalize_emulator(emulator) == "bluestacks" else ADB


def available(emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator) if emulator else "all"
    if emu == "ldplayer":
        return os.path.isfile(LDCONSOLE) and os.path.isfile(ADB)
    if emu == "bluestacks":
        return os.path.isfile(BLUESTACKS_ADB)
    return available("ldplayer") or available("bluestacks")


def list_instances(emulator: str | None = None) -> list[dict]:
    emu = normalize_emulator(emulator) if emulator else "all"
    rows: list[dict] = []
    if emu in ("all", "ldplayer"):
        rows.extend(_list_ldplayer_instances())
    if emu in ("all", "bluestacks"):
        rows.extend(_list_bluestacks_instances())
    return rows


def _list_ldplayer_instances() -> list[dict]:
    rc, out, _ = _run([LDCONSOLE, "list2"])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        rows.append({
            "emulator": "ldplayer",
            "index": index,
            "serial": serial_for(index, "ldplayer"),
            "title": parts[1],
            "running": parts[4] == "1",
            "width": int(parts[7]) if len(parts) > 7 and parts[7].isdigit() else None,
            "height": int(parts[8]) if len(parts) > 8 and parts[8].isdigit() else None,
        })
    return rows


def _list_bluestacks_instances() -> list[dict]:
    if not available("bluestacks"):
        return []
    serial = serial_for(0, "bluestacks")
    return [{
        "emulator": "bluestacks",
        "index": 0,
        "serial": serial,
        "title": "BlueStacks",
        "running": adb_ready(serial, "bluestacks"),
        "width": None,
        "height": None,
    }]


def launch_instance(index: int = 0, emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator)
    if emu == "bluestacks":
        if not os.path.isfile(BLUESTACKS_PLAYER):
            return False
        args = [BLUESTACKS_PLAYER]
        if BLUESTACKS_INSTANCE:
            args += ["--instance", BLUESTACKS_INSTANCE]
        try:
            subprocess.Popen(args, creationflags=_CREATE_NO_WINDOW,
                             cwd=os.path.dirname(BLUESTACKS_PLAYER))
            return True
        except OSError:
            return False
    rc, _, _ = _run([LDCONSOLE, "launch", "--index", str(index)], timeout=15)
    return rc == 0


def serial_for(index: int = 0, emulator: str | None = None) -> str:
    emu = normalize_emulator(emulator)
    if emu == "bluestacks":
        return BLUESTACKS_SERIAL
    return f"emulator-{5554 + index * 2}"


def _ensure_connected(serial: str, emulator: str) -> None:
    if normalize_emulator(emulator) == "bluestacks":
        _run([adb_path_for(emulator), "connect", serial], timeout=8)


def adb_ready(serial: str, emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    rc, out, _ = _run([adb_path_for(emu), "-s", serial, "shell", "getprop",
                       "sys.boot_completed"])
    return rc == 0 and out.strip() == "1"


def screenshot(serial: str, emulator: str | None = None) -> bytes | None:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    adb = adb_path_for(emu)
    _ensure_connected(serial, emu)
    dev = "/sdcard/_agt_cap.png"
    rc, _, _ = _run([adb, "-s", serial, "shell", "screencap", "-p", dev])
    if rc != 0:
        return None
    rc, data, _ = _run([adb, "-s", serial, "exec-out", "cat", dev], binary=True)
    if rc != 0 or not data:
        return None
    return data


def tap(serial: str, x: int, y: int, emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    rc, _, _ = _run([adb_path_for(emu), "-s", serial, "shell", "input", "tap",
                     str(x), str(y)])
    return rc == 0


def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300,
          emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    rc, _, _ = _run([adb_path_for(emu), "-s", serial, "shell", "input", "swipe",
                     str(x1), str(y1), str(x2), str(y2), str(ms)])
    return rc == 0


def launch_app(serial: str, package: str, emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    rc, _, _ = _run([adb_path_for(emu), "-s", serial, "shell", "monkey", "-p", package,
                     "-c", "android.intent.category.LAUNCHER", "1"])
    return rc == 0


def stop_app(serial: str, package: str, emulator: str | None = None) -> bool:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    rc, _, _ = _run([adb_path_for(emu), "-s", serial, "shell", "am", "force-stop",
                     package])
    return rc == 0


def list_packages(serial: str, user_only: bool = True,
                  emulator: str | None = None) -> list[str]:
    emu = normalize_emulator(emulator or emulator_for_serial(serial))
    _ensure_connected(serial, emu)
    args = [adb_path_for(emu), "-s", serial, "shell", "pm", "list", "packages"]
    if user_only:
        args.append("-3")
    rc, out, _ = _run(args)
    if rc != 0:
        return []
    return sorted(l.replace("package:", "").strip() for l in out.splitlines() if l.strip())
