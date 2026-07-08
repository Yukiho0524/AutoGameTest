"""Friendly launcher for AutoGameTest.

This script is intentionally stdlib-only. start.bat only has to find Python;
all environment checks, browser opening, and readable error handling live here.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import traceback
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:8777"
LOG_FILE = os.path.join(ROOT, "data", "logs", "startup.log")
CREATE_NO_WINDOW = 0x08000000


def log(message: str) -> None:
    print(message)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def port_open(host: str = "127.0.0.1", port: int = 8777) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def run_doctor() -> int:
    log("[AutoGameTest] Running environment check...")
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "doctor.py")],
                          cwd=ROOT)
    return proc.returncode


def open_browser() -> None:
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def main() -> int:
    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    log("")
    log(f"[AutoGameTest] Project: {ROOT}")
    log(f"[AutoGameTest] Python: {sys.executable}")

    rc = run_doctor()
    if rc != 0:
        log("")
        log("[ERROR] Environment check failed. See the messages above.")
        log(f"[ERROR] Startup log: {LOG_FILE}")
        return rc

    if port_open():
        log("")
        log(f"[AutoGameTest] Control panel already appears to be running: {URL}")
        open_browser()
        return 0

    log("")
    log("[AutoGameTest] Starting control panel...")
    cmd = [sys.executable, os.path.join(ROOT, "server.py")]
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, creationflags=CREATE_NO_WINDOW)
    except Exception:
        log("[ERROR] Failed to start server.py")
        log(traceback.format_exc())
        log(f"[ERROR] Startup log: {LOG_FILE}")
        return 1

    for _ in range(30):
        if proc.poll() is not None:
            log(f"[ERROR] server.py exited early with code {proc.returncode}")
            log(f"[ERROR] Run manually for details: {sys.executable} server.py")
            log(f"[ERROR] Startup log: {LOG_FILE}")
            return proc.returncode or 1
        if port_open():
            log(f"[AutoGameTest] Control panel is ready: {URL}")
            open_browser()
            return 0
        time.sleep(0.2)

    log("[ERROR] server.py did not become ready within 6 seconds.")
    log(f"[ERROR] Startup log: {LOG_FILE}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

