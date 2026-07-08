"""Friendly launcher for AutoGameTest.

This script is intentionally stdlib-only. start.bat only has to find Python;
all environment checks, browser opening, and readable error handling live here.
"""
from __future__ import annotations

import os
import json
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:8777"
DIAGNOSTICS_URL = URL + "/api/diagnostics"
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


def diagnostics_ready() -> tuple[bool, str, dict]:
    req = urllib.request.Request(
        DIAGNOSTICS_URL,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            text = resp.read(200000).decode("utf-8", "replace")
            data = json.loads(text)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from {DIAGNOSTICS_URL}", {}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return False, f"{type(e).__name__}: {e}", {}
    if isinstance(data, dict) and isinstance(data.get("checks"), list):
        return True, "diagnostics API is ready", data
    return False, "diagnostics API returned unexpected JSON", {}


def parse_server_started(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def latest_runtime_mtime() -> float:
    files = [
        os.path.join(ROOT, "server.py"),
        os.path.join(ROOT, "core", "adb.py"),
        os.path.join(ROOT, "core", "config.py"),
        os.path.join(ROOT, "web", "app.js"),
        os.path.join(ROOT, "web", "style.css"),
        os.path.join(ROOT, "config", "local.json"),
    ]
    mtimes = []
    for path in files:
        if not os.path.isfile(path):
            continue
        try:
            mtimes.append(os.path.getmtime(path))
        except OSError:
            pass
    return max(mtimes or [0])


def running_server_is_current(data: dict) -> tuple[bool, str]:
    project = os.path.abspath(str(data.get("project", "") or ""))
    if os.path.normcase(project) != os.path.normcase(ROOT):
        return False, f"running server project is {project or '(unknown)'}, expected {ROOT}"

    started_at = str(data.get("system", {}).get("server_started_at", "") or "")
    started_ts = parse_server_started(started_at)
    if not started_ts:
        return False, "running server does not expose server_started_at; treating it as an older version"

    latest_mtime = latest_runtime_mtime()
    if latest_mtime > started_ts + 1:
        changed = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return False, f"project files/local config changed at {changed}, after server start {started_at}"
    return True, f"running server is current; started at {started_at}"


def pids_listening_on_port(port: int = 8777) -> list[int]:
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local, _remote, state, pid = parts[:5]
        if proto.upper() != "TCP" or state.upper() != "LISTENING":
            continue
        if not local.endswith(f":{port}"):
            continue
        try:
            value = int(pid)
        except ValueError:
            continue
        if value not in pids:
            pids.append(value)
    return pids


def process_command_line(pid: int) -> str:
    ps = (
        "try { "
        f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\").CommandLine "
        "} catch { '' }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def looks_like_autogametest_server(command_line: str) -> bool:
    text = (command_line or "").lower()
    root = ROOT.lower()
    normalized = text.replace('"', "").strip()
    relative_server_py = normalized.endswith(" server.py") or normalized == "server.py"
    return (
        "server.py" in text
        and ("python" in text or "py.exe" in text or "py " in text)
        and ("autogametest" in text or root in text or relative_server_py)
    )


def stop_stale_server() -> bool:
    stopped = False
    for pid in pids_listening_on_port(8777):
        cmd = process_command_line(pid)
        log(f"[AutoGameTest] Port 8777 is owned by PID {pid}: {cmd or '(unknown command line)'}")
        if not looks_like_autogametest_server(cmd):
            log("[AutoGameTest] Existing process does not look like AutoGameTest server.py; leaving it alone.")
            continue
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            stopped = True
            log(f"[AutoGameTest] Stopped stale AutoGameTest server PID {pid}.")
        except Exception as e:
            log(f"[AutoGameTest] Failed to stop PID {pid}: {e}")
    if stopped:
        for _ in range(20):
            if not port_open():
                return True
            time.sleep(0.2)
    return stopped and not port_open()


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
        ready, detail, data = diagnostics_ready()
        log("")
        stale_reason = detail
        if ready:
            current, reason = running_server_is_current(data)
            if current:
                log(f"[AutoGameTest] Control panel already appears to be running: {URL}")
                log(f"[AutoGameTest] {reason}")
                open_browser()
                return 0
            log("[AutoGameTest] Existing control panel is stale.")
            log(f"[AutoGameTest] {reason}")
            stale_reason = reason
        log("[AutoGameTest] Port 8777 is occupied, but the running server is not compatible with this version.")
        log(f"[AutoGameTest] Reason: {stale_reason}")
        log("[AutoGameTest] Trying to stop stale server.py and start the latest version...")
        if not stop_stale_server():
            log("[ERROR] Could not stop the process using port 8777.")
            log("[ERROR] Close the old AutoGameTest/python window or reboot, then run start.bat again.")
            return 1

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
        ready, detail, _data = diagnostics_ready() if port_open() else (False, "port not open", {})
        if ready:
            log(f"[AutoGameTest] Control panel is ready: {URL}")
            open_browser()
            return 0
        if port_open():
            log(f"[AutoGameTest] Waiting for diagnostics API: {detail}")
        time.sleep(0.2)

    log("[ERROR] server.py did not become ready within 6 seconds.")
    log(f"[ERROR] Startup log: {LOG_FILE}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
