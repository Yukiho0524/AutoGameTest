"""AutoGameTest control panel — a dependency-free (stdlib only) local web server.

Run:  python server.py     then open  http://127.0.0.1:8777

The server handles all *mechanical* work: game config, platform detection,
launching, and live emulator control (screenshot + tap). AI cognition
(learning a game, playing as an agent) is executed through Codex job runners.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core import store, platforms, launcher, adb

ROOT = os.path.dirname(os.path.abspath(__file__))
_CREATE_NO_WINDOW = 0x08000000
WEB_DIR = os.path.join(ROOT, "web")
LOG_DIR = os.path.join(ROOT, "data", "logs")
HOST, PORT = "127.0.0.1", 8777
_scheduler_started = False


def spawn_runner(script_name: str, job_id: str, engine: str = "codex") -> bool:
    runner = os.path.join(ROOT, "tools", script_name)
    if not os.path.isfile(runner):
        return False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        out_path = os.path.join(LOG_DIR, f"{job_id}.out.log")
        err_path = os.path.join(LOG_DIR, f"{job_id}.err.log")
        out = open(out_path, "w", encoding="utf-8")
        err = open(err_path, "w", encoding="utf-8")
        try:
            subprocess.Popen(
                [sys.executable, runner, "--job", job_id, "--engine", engine],
                cwd=ROOT, creationflags=_CREATE_NO_WINDOW,
                stdout=out, stderr=err,
                stdin=subprocess.DEVNULL,
            )
        finally:
            out.close()
            err.close()
        store.update_job(job_id, log_stdout=out_path, log_stderr=err_path)
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


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoGameTest/0.1"

    # ---- helpers ----
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
            return self._json({"jobs": store.list_jobs()})
        if p == "/api/schedules":
            return self._json({"schedules": store.list_schedules()})
        if p == "/api/emulator/instances":
            return self._json({"available": adb.available(),
                               "instances": adb.list_instances()})
        if p == "/api/emulator/packages":
            serial = q.get("serial", ["emulator-5554"])[0]
            return self._json({"packages": adb.list_packages(serial)})
        if p == "/api/emulator/screenshot":
            serial = q.get("serial", ["emulator-5554"])[0]
            png = adb.screenshot(serial)
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
        m = re.match(r"^/api/games/([^/]+)/launch$", p)
        if m:
            g = store.get_game(m.group(1))
            if not g:
                return self.send_error(404)
            return self._json(launcher.launch(g))
        if p == "/api/emulator/tap":
            ok = adb.tap(b.get("serial", "emulator-5554"), int(b["x"]), int(b["y"]))
            return self._json({"ok": ok})
        if p == "/api/emulator/launch-instance":
            adb.launch_instance(int(b.get("index", 0)))
            return self._json({"ok": True})
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
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AutoGameTest control panel: http://{HOST}:{PORT}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
