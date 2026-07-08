"""Environment checker for AutoGameTest."""
from __future__ import annotations

import os
import socket
import sys
from shutil import which

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import adb, config  # noqa: E402


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def path_status(label: str, path: str) -> bool:
    if path and os.path.isfile(path):
        ok(f"{label}: {path}")
        return True
    warn(f"{label}: not found")
    return False


def check_python() -> bool:
    version = sys.version_info
    if version >= (3, 10):
        ok(f"Python {version.major}.{version.minor}.{version.micro}")
        return True
    fail(f"Python {version.major}.{version.minor}.{version.micro}; need 3.10+")
    return False


def check_writable(path: str) -> bool:
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".write-test.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        ok(f"Writable: {path}")
        return True
    except OSError as e:
        fail(f"Not writable: {path} ({e})")
        return False


def check_port(host: str = "127.0.0.1", port: int = 8777) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        result = sock.connect_ex((host, port))
    finally:
        sock.close()
    if result == 0:
        warn(f"Port {port} is already in use. server.py may already be running.")
    else:
        ok(f"Port {port} is available")
    return True


def check_cli(label: str, path: str | None, executable_name: str) -> bool:
    if path and os.path.isfile(path):
        ok(f"{label}: {path}")
        return True
    found = which(executable_name)
    if found:
        ok(f"{label}: {found}")
        return True
    fail(f"{label}: not found")
    return False


def main() -> int:
    print("AutoGameTest environment check")
    print(f"Project: {ROOT}")
    print()

    required_ok = True
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    required_ok &= check_python()
    required_ok &= check_writable(os.path.join(ROOT, "data"))
    check_port()

    print()
    print("Emulator tools")
    local_status = config.status()
    if os.path.isfile(config.LOCAL_CONFIG) and local_status.get("format") == "json":
        keys = ", ".join(local_status.get("keys", [])) or "no values"
        ok(f"Local config: {config.LOCAL_CONFIG} ({keys})")
    elif os.path.isfile(config.LOCAL_CONFIG) and local_status.get("format") == "lenient":
        keys = ", ".join(local_status.get("keys", [])) or "no values"
        warn(f"Local config uses lenient parsing: {config.LOCAL_CONFIG} ({keys})")
        warn(r"Use \\ or / in Windows paths to make local.json valid JSON.")
    elif os.path.isfile(config.LOCAL_CONFIG):
        fail(f"Local config read failed: {local_status.get('error', 'unknown error')}")
        required_ok = False
    else:
        warn("config/local.json not found; using auto-detection/default paths")
        warn("If paths differ on this PC, copy config.example.json to config/local.json")
    path_status("LDPlayer ldconsole", adb.LDCONSOLE)
    path_status("LDPlayer adb", adb.ADB)
    path_status("BlueStacks player", adb.BLUESTACKS_PLAYER)
    path_status("BlueStacks adb", adb.BLUESTACKS_ADB)
    if not adb.available():
        warn("No emulator ADB backend found. Install LDPlayer/BlueStacks or set paths in config/local.json.")

    print()
    print("AI CLIs")
    try:
        from tools import ai_runner
    except ImportError:
        import ai_runner  # type: ignore
    required_ok &= check_cli("Codex CLI", ai_runner.find_codex(), "codex")
    if not required_ok:
        warn("Install Codex or set codex_path in config/local.json")

    print()
    if required_ok:
        ok("Required checks passed")
        return 0
    fail("Required checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
