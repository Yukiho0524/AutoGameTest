"""Environment checker for AutoGameTest."""
from __future__ import annotations

import os
import socket
import subprocess
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


def check_cli(label: str, path: str | None, fallback_name: str) -> bool:
    if path and os.path.isfile(path):
        ok(f"{label}: {path}")
        return True
    found = which(fallback_name)
    if found:
        ok(f"{label}: {found}")
        return True
    warn(f"{label}: not found; related AI fallback may not work")
    return False


def main() -> int:
    print("AutoGameTest environment check")
    print(f"Project: {ROOT}")
    print()

    required_ok = True
    required_ok &= check_python()
    required_ok &= check_writable(os.path.join(ROOT, "data"))
    check_port()

    print()
    print("Emulator tools")
    if os.path.isfile(config.LOCAL_CONFIG):
        ok(f"Local config: {config.LOCAL_CONFIG}")
    else:
        warn("config/local.json not found; using auto-detection/default paths")
        warn("Copy config.example.json to config/local.json to set machine-specific paths")
    path_status("LDPlayer ldconsole", adb.LDCONSOLE)
    path_status("LDPlayer adb", adb.ADB)
    if not adb.available():
        warn("LDPlayer is optional unless you use Android emulator agents")

    print()
    print("AI CLIs")
    try:
        from tools import ai_runner
    except ImportError:
        import ai_runner  # type: ignore
    check_cli("Claude Code", ai_runner.find_claude(), "claude")
    check_cli("Codex CLI", ai_runner.find_codex(), "codex")

    print()
    if required_ok:
        ok("Required checks passed")
        return 0
    fail("Required checks failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

