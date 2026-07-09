"""AutoGameTest control panel — a dependency-free (stdlib only) local web server.

Run:  python server.py     then open  http://127.0.0.1:8777

The server handles all *mechanical* work: game config, platform detection,
launching, and live emulator control (screenshot + tap). AI cognition
(learning a game, playing as an agent) is executed through Codex job runners.
"""
from __future__ import annotations

import json
import os
import platform as py_platform
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core import store, platforms, launcher, adb, config, recorder

ROOT = os.path.dirname(os.path.abspath(__file__))
_CREATE_NO_WINDOW = 0x08000000
WEB_DIR = os.path.join(ROOT, "web")
LOG_DIR = os.path.join(ROOT, "data", "logs")
HOST, PORT = "127.0.0.1", 8777
_scheduler_started = False
SERVER_STARTED_AT = datetime.now()


def _format_epoch(value: str | float | int | None) -> str:
    if value in ("", None):
        return "未知"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "未知"


def ai_timeout_seconds() -> int:
    return int(store.get_settings().get("ai_timeout_seconds", 3600))


def ai_codex_settings() -> dict:
    settings = store.get_settings()
    return {
        "model": str(settings.get("codex_model", "gpt-5.5") or "gpt-5.5").strip(),
        "reasoning_effort": str(
            settings.get("codex_reasoning_effort", "high") or "high"
        ).strip().lower(),
    }


def _pid_exists(pid: int | str | None) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def reconcile_running_jobs() -> None:
    for job in store.list_jobs():
        if job.get("status") != "running":
            continue
        pid = job.get("runner_pid")
        if not pid or _pid_exists(pid):
            continue
        store.update_job(
            job["id"],
            status="error",
            result="Runner process exited before writing a result. Check stdout/stderr logs, then re-run the Agent.",
            run_reason="runner process disappeared",
            error_trace=f"Recorded runner_pid={pid} is no longer running.",
        )


def spawn_runner(script_name: str, job_id: str, engine: str = "codex",
                 timeout: int | None = None) -> bool:
    runner = os.path.join(ROOT, "tools", script_name)
    if not os.path.isfile(runner):
        return False
    timeout = int(timeout or ai_timeout_seconds())
    codex_settings = ai_codex_settings()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        out_path = os.path.join(LOG_DIR, f"{job_id}.out.log")
        err_path = os.path.join(LOG_DIR, f"{job_id}.err.log")
        out = open(out_path, "w", encoding="utf-8")
        err = open(err_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [sys.executable, runner, "--job", job_id, "--engine", engine,
                 "--timeout", str(timeout),
                 "--model", codex_settings["model"],
                 "--reasoning-effort", codex_settings["reasoning_effort"]],
                cwd=ROOT, creationflags=_CREATE_NO_WINDOW,
                stdout=out, stderr=err,
                stdin=subprocess.DEVNULL,
            )
        finally:
            out.close()
            err.close()
        store.update_job(job_id, log_stdout=out_path, log_stderr=err_path,
                         ai_timeout_seconds=timeout,
                         runner_pid=proc.pid,
                         runner_started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         codex_model=codex_settings["model"],
                         codex_reasoning_effort=codex_settings["reasoning_effort"])
        return True
    except Exception:
        return False


def enqueue_agent_run(agent: dict, source: str = "manual",
                      schedule: dict | None = None,
                      engine: str = "codex") -> dict:
    payload = {
        "agent_id": agent["id"],
        "game_id": agent.get("game_id"),
        "prompt": agent.get("prompt", ""),
        "source": source,
    }
    if schedule:
        payload["schedule_id"] = schedule.get("id")
        payload["scheduled_day"] = schedule.get("day")
        payload["scheduled_hour"] = schedule.get("hour")
        payload["scheduled_minute"] = schedule.get("minute", 0)
    job = store.enqueue_job("run_agent", payload)
    spawned = spawn_runner("run_agent.py", job["id"], engine)
    job["spawned"] = spawned
    if not spawned:
        store.update_job(
            job["id"],
            status="error",
            result="無法啟動 tools/run_agent.py 背景執行器")
    return job


def _scheduler_loop() -> None:
    while True:
        now = datetime.now()
        run_key = now.strftime("%Y-%m-%d-%H-%M")
        for schedule in store.list_schedules():
            if not schedule.get("enabled", True):
                continue
            if schedule.get("last_run_key") == run_key:
                continue
            if int(schedule.get("day", -1)) != now.weekday():
                continue
            if int(schedule.get("hour", -1)) != now.hour:
                continue
            if int(schedule.get("minute", 0)) != now.minute:
                continue
            agent = store.get_agent(schedule.get("agent_id", ""))
            if not agent:
                store.mark_schedule_run(schedule.get("id", ""), run_key)
                continue
            enqueue_agent_run(agent, source="schedule", schedule=schedule)
            store.mark_schedule_run(schedule.get("id", ""), run_key)
        time.sleep(20)


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, name="schedule-runner", daemon=True)
    t.start()


def _safe_log_path(path: str | None) -> str | None:
    if not path:
        return None
    full = os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))
    log_root = os.path.abspath(LOG_DIR)
    try:
        if os.path.commonpath([log_root, full]) != log_root:
            return None
    except ValueError:
        return None
    return full


def _tail_log(path: str | None, limit: int = 40000) -> dict:
    full = _safe_log_path(path)
    info = {
        "path": full or path or "",
        "exists": False,
        "size": 0,
        "tail": "",
        "truncated": False,
        "mtime": "",
    }
    if not full or not os.path.isfile(full):
        return info
    size = os.path.getsize(full)
    info["exists"] = True
    info["size"] = size
    info["mtime"] = datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S")
    with open(full, "rb") as f:
        if size > limit:
            f.seek(-limit, os.SEEK_END)
            info["truncated"] = True
        data = f.read()
    text = data.decode("utf-8", "replace")
    if info["truncated"]:
        text = "... log truncated; showing latest output ...\n" + text
    info["tail"] = text
    return info


def _recent_logs(limit: int = 16) -> list[dict]:
    if not os.path.isdir(LOG_DIR):
        return []
    rows = []
    for name in os.listdir(LOG_DIR):
        full = os.path.join(LOG_DIR, name)
        if not os.path.isfile(full):
            continue
        rows.append({
            "name": name,
            "path": full,
            "size": os.path.getsize(full),
            "mtime": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%Y-%m-%d %H:%M:%S"),
        })
    rows.sort(key=lambda x: x["mtime"], reverse=True)
    return rows[:limit]


def _check(level: str, key: str, title: str, detail: str, action: str = "") -> dict:
    return {"level": level, "key": key, "title": title, "detail": detail, "action": action}


def _log_diagnostics_error(title: str, exc: BaseException) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "diagnostics.err.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {title}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass


def _safe_check(key: str, title: str, fn) -> dict:
    try:
        return fn()
    except Exception as e:
        _log_diagnostics_error(title, e)
        return _check(
            "fail",
            key,
            title,
            f"診斷檢查失敗：{type(e).__name__}: {e}",
            "請查看 data/logs/diagnostics.err.log，或把診斷頁截圖回報",
        )


def _file_check(key: str, title: str, path: str, required: bool = False) -> dict:
    if path and os.path.isfile(path):
        return _check("ok", key, title, path)
    level = "fail" if required else "warn"
    return _check(level, key, title, path or "未設定", "確認安裝位置或寫入 config/local.json")


def _data_writable_check() -> dict:
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    probe = os.path.join(data_dir, ".diagnostic-write-test.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return _check("ok", "data_writable", "資料目錄可寫入", data_dir)
    except OSError as e:
        return _check("fail", "data_writable", "資料目錄不可寫入", f"{data_dir} ({e})",
                      "請確認專案資料夾權限")


def _port_check() -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.5)
        in_use = sock.connect_ex((HOST, PORT)) == 0
    finally:
        sock.close()
    if in_use:
        return _check("ok", "port", f"控制台 Port {PORT}", f"http://{HOST}:{PORT} 已啟用")
    return _check("warn", "port", f"控制台 Port {PORT}", "目前未偵測到服務",
                  "若頁面仍可操作，重新整理診斷即可")


def _codex_check() -> dict:
    try:
        from tools import ai_runner
        path = ai_runner.find_codex()
    except Exception as e:
        return _check("fail", "codex", "Codex CLI", f"偵測失敗：{e}",
                      "安裝 Codex 或在 config/local.json 設定 codex_path")
    if path and os.path.isfile(path):
        return _check("ok", "codex", "Codex CLI", path)
    return _check("fail", "codex", "Codex CLI", "找不到 codex.exe",
                  "安裝 Codex 或在 config/local.json 設定 codex_path")


def _job_status_check() -> dict:
    reconcile_running_jobs()
    counts: dict[str, int] = {}
    for job in store.list_jobs():
        status = str(job.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "沒有任務"
    level = "warn" if counts.get("error") else "ok"
    return _check(level, "jobs", "任務佇列狀態", detail)


def _settings_check() -> dict:
    timeout = ai_timeout_seconds()
    codex_settings = ai_codex_settings()
    return _check(
        "ok",
        "ai_timeout",
        "AI 任務設定",
        (
            f"timeout {timeout} 秒（約 {timeout // 60} 分鐘）；"
            f"Codex {codex_settings['model']} + {codex_settings['reasoning_effort']}"
        ),
    )


def _local_emulator_values() -> str:
    local = config.load()
    keys = [
        "ldplayer_dir",
        "ldconsole_path",
        "adb_path",
        "bluestacks_dir",
        "bluestack_dir",
        "bluestacks_player_path",
        "bluestack_player_path",
        "bluestacks_adb_path",
        "bluestack_adb_path",
    ]
    values = []
    for key in keys:
        value = str(local.get(key, "") or "").strip()
        if value:
            values.append(f"{key}={value}")
    return "；".join(values)


def build_diagnostics() -> dict:
    adb.reload_config_paths()
    checks = []
    version = sys.version_info
    checks.append(_check(
        "ok" if version >= (3, 10) else "fail",
        "python",
        "Python 版本",
        f"{version.major}.{version.minor}.{version.micro} ({sys.executable})",
        "" if version >= (3, 10) else "請安裝 Python 3.10 以上並加入 PATH",
    ))
    checks.append(_safe_check("data_writable", "資料目錄可寫入", _data_writable_check))
    checks.append(_safe_check("port", f"控制台 Port {PORT}", _port_check))
    checks.append(_safe_check("codex", "Codex CLI", _codex_check))
    checks.append(_safe_check("ai_timeout", "AI 任務 timeout", _settings_check))
    checks.append(_safe_check(
        "start_bat",
        "Windows 啟動檔",
        lambda: _file_check("start_bat", "Windows 啟動檔", os.path.join(ROOT, "start.bat"), True),
    ))

    local_config = os.path.join(ROOT, "config", "local.json")
    local_status = config.status()
    local_mtime = _format_epoch(local_status.get("mtime"))
    if os.path.isfile(local_config) and local_status.get("format") == "json":
        keys = ", ".join(local_status.get("keys", [])) or "沒有設定值"
        checks.append(_check(
            "ok",
            "local_config",
            "本機設定檔",
            f"{local_config}（json，修改：{local_mtime}）",
            f"已讀取：{keys}",
        ))
    elif os.path.isfile(local_config) and local_status.get("format") == "lenient":
        keys = ", ".join(local_status.get("keys", [])) or "沒有設定值"
        checks.append(_check(
            "warn",
            "local_config",
            "本機設定檔",
            f"{local_config}（寬容讀取，修改：{local_mtime}）",
            f"已讀取：{keys}。local.json 不是標準 JSON；Windows 路徑請使用 \\\\ 或 /",
        ))
    elif os.path.isfile(local_config):
        checks.append(_check(
            "fail",
            "local_config",
            "本機設定檔",
            f"{local_config} 讀取失敗（修改：{local_mtime}）：{local_status.get('error', 'unknown error')}",
            "請檢查 JSON 格式；Windows 路徑請使用 \\\\ 或 /",
        ))
    else:
        checks.append(_check("warn", "local_config", "本機設定檔", "尚未建立 config/local.json",
                             "路徑不一致時可由 config.example.json 複製建立"))

    local_emulator_values = _local_emulator_values()
    if local_emulator_values:
        checks.append(_check(
            "info",
            "local_emulator_values",
            "local 模擬器設定值",
            local_emulator_values,
            "下方 LDPlayer / BlueStacks 卡片會顯示目前實際採用的解析後路徑",
        ))

    ld_ok = False
    bs_ok = False
    checks.append(_safe_check(
        "ldconsole", "LDPlayer ldconsole",
        lambda: _file_check("ldconsole", "LDPlayer ldconsole", adb.LDCONSOLE),
    ))
    checks.append(_safe_check(
        "ld_adb", "LDPlayer ADB",
        lambda: _file_check("ld_adb", "LDPlayer ADB", adb.ADB),
    ))
    checks.append(_safe_check(
        "bs_player", "BlueStacks Player",
        lambda: _file_check("bs_player", "BlueStacks Player", adb.BLUESTACKS_PLAYER),
    ))
    checks.append(_safe_check(
        "bs_adb", "BlueStacks ADB",
        lambda: _file_check("bs_adb", "BlueStacks ADB", adb.BLUESTACKS_ADB),
    ))
    try:
        ld_ok = adb.available("ldplayer")
        bs_ok = adb.available("bluestacks")
        checks.append(_check(
            "ok" if (ld_ok or bs_ok) else "warn",
            "emulator_backend",
            "模擬器 ADB 後端",
            f"LDPlayer: {'可用' if ld_ok else '未偵測'}；BlueStacks: {'可用' if bs_ok else '未偵測'}",
            "" if (ld_ok or bs_ok) else "安裝模擬器或在 config/local.json 設定路徑",
        ))
    except Exception as e:
        _log_diagnostics_error("模擬器 ADB 後端", e)
        checks.append(_check(
            "fail", "emulator_backend", "模擬器 ADB 後端",
            f"偵測失敗：{type(e).__name__}: {e}",
            "請查看 data/logs/diagnostics.err.log",
        ))
    checks.append(_safe_check("jobs", "任務佇列狀態", _job_status_check))

    counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for c in checks:
        counts[c["level"]] = counts.get(c["level"], 0) + 1
    status = "fail" if counts.get("fail") else "warn" if counts.get("warn") else "ok"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project": ROOT,
        "system": {
            "platform": py_platform.platform(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "server_started_at": SERVER_STARTED_AT.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "summary": {"status": status, "counts": counts},
        "checks": checks,
        "logs": _recent_logs(),
    }


def build_diagnostics_failure(exc: BaseException) -> dict:
    _log_diagnostics_error("diagnostics endpoint", exc)
    checks = [
        _check(
            "fail",
            "diagnostics_endpoint",
            "診斷端點執行失敗",
            f"{type(exc).__name__}: {exc}",
            "請重新啟動控制台；若仍發生，請查看 data/logs/diagnostics.err.log",
        )
    ]
    try:
        logs = _recent_logs()
    except Exception:
        logs = []
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project": ROOT,
        "system": {
            "platform": py_platform.platform(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
        },
        "summary": {"status": "fail", "counts": {"ok": 0, "warn": 0, "fail": 1, "info": 0}},
        "checks": checks,
        "logs": logs,
    }


def job_detail(job_id: str) -> dict | None:
    reconcile_running_jobs()
    job = store.get_job(job_id)
    if not job:
        return None
    return {
        "job": job,
        "logs": {
            "stdout": _tail_log(job.get("log_stdout")),
            "stderr": _tail_log(job.get("log_stderr")),
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoGameTest/0.1"

    # ---- helpers ----
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)  # always read the body so the socket stays sane
        for enc in ("utf-8", "cp950", "latin-1"):
            try:
                return json.loads(raw.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {}

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # keep console quiet

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == "/api/games":
            return self._json({"games": store.list_games()})
        if p == "/api/agents":
            return self._json({"agents": store.list_agents(q.get("game_id", [None])[0])})
        if p == "/api/jobs":
            reconcile_running_jobs()
            return self._json({"jobs": store.list_jobs()})
        if p == "/api/schedules":
            return self._json({"schedules": store.list_schedules()})
        if p == "/api/settings":
            return self._json({"settings": store.get_settings()})
        if p == "/api/diagnostics":
            try:
                return self._json(build_diagnostics())
            except Exception as e:
                return self._json(build_diagnostics_failure(e), status=200)
        m = re.match(r"^/api/jobs/([^/]+)$", p)
        if m:
            detail = job_detail(m.group(1))
            if not detail:
                return self.send_error(404)
            return self._json(detail)
        if p == "/api/emulator/instances":
            adb.reload_config_paths()
            emulator = q.get("emulator", [None])[0]
            return self._json({"available": adb.available(emulator),
                               "instances": adb.list_instances(emulator)})
        if p == "/api/emulator/packages":
            adb.reload_config_paths()
            emulator = q.get("emulator", [None])[0]
            serial = q.get("serial", [adb.serial_for(0, emulator)])[0]
            return self._json({"packages": adb.list_packages(serial, emulator=emulator)})
        if p == "/api/emulator/record/status":
            serial = q.get("serial", [None])[0]
            st = recorder.status(serial)
            saved = store.get_settings().get("recording_dir", "")
            st["default_dir"] = saved or recorder.DEFAULT_SAVE_DIR
            return self._json(st)
        if p == "/api/emulator/screenshot":
            adb.reload_config_paths()
            emulator = q.get("emulator", [None])[0]
            serial = q.get("serial", [adb.serial_for(0, emulator)])[0]
            png = adb.screenshot(serial, emulator)
            if not png:
                return self.send_error(503, "screenshot failed")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)
            return
        if p.startswith("/api/skill/"):
            gid = p.rsplit("/", 1)[-1]
            return self._json({"content": store.read_skill(gid)})
        return self._static(p)

    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        if p == "/api/detect-platform":
            return self._json(platforms.detect_platform(b.get("exe_path", "")))
        if p == "/api/games":
            return self._json(store.upsert_game(b))
        if p == "/api/agents":
            return self._json(store.upsert_agent(b))
        if p == "/api/schedules":
            schedules = b.get("schedules", []) if isinstance(b, dict) else []
            return self._json({"schedules": store.save_schedules(schedules)})
        if p == "/api/settings":
            settings = store.save_settings(b if isinstance(b, dict) else {})
            return self._json({"settings": settings})
        m = re.match(r"^/api/games/([^/]+)/launch$", p)
        if m:
            g = store.get_game(m.group(1))
            if not g:
                return self.send_error(404)
            adb.reload_config_paths()
            return self._json(launcher.launch(g))
        if p == "/api/emulator/tap":
            adb.reload_config_paths()
            serial = b.get("serial", adb.serial_for(0, b.get("emulator")))
            ok = adb.tap(serial, int(b["x"]), int(b["y"]), b.get("emulator"))
            return self._json({"ok": ok})
        if p == "/api/emulator/launch-instance":
            adb.reload_config_paths()
            adb.launch_instance(int(b.get("index", 0)), b.get("emulator"))
            return self._json({"ok": True})
        if p == "/api/emulator/record/start":
            adb.reload_config_paths()
            emulator = b.get("emulator")
            serial = b.get("serial") or adb.serial_for(0, emulator)
            res = recorder.start_recording(
                serial, emulator, b.get("save_dir"),
                show_touches=bool(b.get("show_touches", True)))
            if res.get("ok") and res.get("save_dir"):
                # remember the folder so next recording prefills it
                settings = store.get_settings()
                settings["recording_dir"] = res["save_dir"]
                store.save_settings(settings)
            return self._json(res)
        if p == "/api/emulator/record/stop":
            serial = b.get("serial") or ""
            if not serial:
                st = recorder.status(None)
                serial = st.get("serial", "")
            return self._json(recorder.stop_recording(serial))
        if p == "/api/emulator/record/open-folder":
            folder = recorder.resolve_save_dir(
                b.get("dir") or store.get_settings().get("recording_dir", ""))
            if not os.path.isdir(folder):
                return self._json({"ok": False, "error": f"資料夾不存在：{folder}"})
            try:
                os.startfile(folder)  # opens Windows Explorer locally
                return self._json({"ok": True, "dir": folder})
            except OSError as e:
                return self._json({"ok": False, "error": str(e)})
        m = re.match(r"^/api/games/([^/]+)/learn$", p)
        if m:
            job = store.enqueue_job("learn", {
                "game_id": m.group(1),
                "sources": b.get("sources", []),
            })
            mode = (b or {}).get("engine", "codex")
            spawned = spawn_runner("run_learn.py", job["id"], mode)
            job["spawned"] = spawned
            if not spawned:
                store.update_job(
                    job["id"],
                    status="error",
                    result="無法啟動 tools/run_learn.py 背景執行器")
            return self._json(job)
        m = re.match(r"^/api/agents/([^/]+)/run$", p)
        if m:
            a = next((x for x in store.list_agents() if x["id"] == m.group(1)), None)
            if not a:
                return self.send_error(404)
            mode = (b or {}).get("engine", "codex")
            return self._json(enqueue_agent_run(a, engine=mode))
        return self.send_error(404)

    def do_DELETE(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        m = re.match(r"^/api/games/([^/]+)$", p)
        if m:
            return self._json({"ok": store.delete_game(m.group(1))})
        m = re.match(r"^/api/agents/([^/]+)$", p)
        if m:
            return self._json({"ok": store.delete_agent(m.group(1))})
        if p == "/api/jobs":
            scope = q.get("scope", ["finished"])[0]
            return self._json({"ok": True, "removed": store.clear_jobs(scope)})
        m = re.match(r"^/api/jobs/([^/]+)$", p)
        if m:
            return self._json({"ok": store.delete_job(m.group(1))})
        return self.send_error(404)


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    start_scheduler()
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"AutoGameTest control panel failed to start on http://{HOST}:{PORT}")
        print(f"Reason: {e}")
        print("If another AutoGameTest window is already running, open that URL instead.")
        return 1
    print(f"AutoGameTest control panel: http://{HOST}:{PORT}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
