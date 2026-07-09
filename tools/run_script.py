"""Replay a generated script with plain ADB — no AI involved.

This is the execution half of the 腳本 feature: generation needs Codex, but
replay is deterministic coordinate playback. Runs as a job runner (same
spawn contract as run_agent.py) or standalone via --script.

Usage:
    python tools/run_script.py --script <script_id>
    python tools/run_script.py --job <job_id>
    python tools/run_script.py --script <id> --serial emulator-5554
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import store, adb, scripts  # noqa: E402

ARTIFACTS_DIR = os.path.join(ROOT, "data", "artifacts")


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height from a PNG's IHDR (stdlib-only; no cv2 needed)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return 0, 0


class ScriptRunner:
    def __init__(self, script: dict, serial: str = "", emulator: str = "",
                 job_id: str | None = None):
        self.script = script
        self.emulator = adb.normalize_emulator(
            emulator or script.get("emulator") or "ldplayer")
        self.serial = (serial or script.get("serial")
                       or adb.serial_for(0, self.emulator))
        self.job_id = job_id
        self.width, self.height = 0, 0
        self.steps_done = 0
        self.shots: list[str] = []
        self.art_dir = ""
        if job_id:
            self.art_dir = os.path.join(ARTIFACTS_DIR, job_id)
            os.makedirs(self.art_dir, exist_ok=True)

    # ---- helpers ----
    def _progress(self, text: str) -> None:
        print(text, flush=True)
        if self.job_id:
            store.update_job(self.job_id, progress=text)

    def _screenshot(self, tag: str) -> None:
        if not self.art_dir:
            return
        png = adb.screenshot(self.serial, self.emulator)
        if not png:
            return
        if not self.width:
            self.width, self.height = _png_size(png)
        path = os.path.join(self.art_dir, f"{tag}.png")
        try:
            with open(path, "wb") as f:
                f.write(png)
            self.shots.append(path)
        except OSError:
            pass

    def _resolve_size(self) -> bool:
        png = adb.screenshot(self.serial, self.emulator)
        if not png:
            return False
        self.width, self.height = _png_size(png)
        return self.width > 0 and self.height > 0

    def _px(self, nx, ny) -> tuple[int, int]:
        x = int(round(float(nx) * (self.width - 1)))
        y = int(round(float(ny) * (self.height - 1)))
        return max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y))

    # ---- run ----
    def run(self) -> dict:
        err = scripts.validate_script(self.script)
        if err:
            return {"ok": False, "error": f"腳本無效：{err}"}
        if not adb.adb_ready(self.serial, self.emulator):
            return {"ok": False,
                    "error": f"裝置 {self.serial} 尚未就緒（模擬器未開機？）"}
        if not self._resolve_size():
            return {"ok": False, "error": "無法取得裝置畫面尺寸（截圖失敗）"}

        steps = self.script.get("steps") or []
        total = len(steps)
        started = time.time()
        self._progress(f"開始執行腳本「{self.script.get('name','')}」"
                       f"（{total} 步，裝置 {self.serial}，"
                       f"畫面 {self.width}x{self.height}）")
        for i, s in enumerate(steps, 1):
            action = s.get("action")
            name = s.get("name") or action
            self._progress(f"[{i}/{total}] {name}")
            ok = self._exec_step(s)
            self._screenshot(f"step_{i:02d}")
            if not ok:
                elapsed = round(time.time() - started, 1)
                return {"ok": False, "steps_done": self.steps_done,
                        "total_steps": total, "elapsed": elapsed,
                        "error": f"step {i}（{name}）執行失敗（adb 指令未成功）"}
            self.steps_done = i
            wait_after = float(s.get("wait_after", 0) or 0)
            if wait_after > 0:
                time.sleep(min(wait_after, 120))
        elapsed = round(time.time() - started, 1)
        return {"ok": True, "steps_done": self.steps_done,
                "total_steps": total, "elapsed": elapsed}

    def _exec_step(self, s: dict) -> bool:
        action = s.get("action")
        if action == "wait":
            time.sleep(min(float(s.get("seconds", 1) or 1), 300))
            return True
        if action == "launch_app":
            package = s.get("package") or self.script.get("package")
            if not package:
                return True   # nothing to launch is not a failure
            return adb.launch_app(self.serial, package, self.emulator)
        if action == "tap":
            x, y = self._px(s["x"], s["y"])
            return adb.tap(self.serial, x, y, self.emulator)
        if action == "long_press":
            x, y = self._px(s["x"], s["y"])
            ms = max(400, int(s.get("duration_ms", 600)))
            # same-point swipe with duration = long press
            return adb.swipe(self.serial, x, y, x, y, ms, self.emulator)
        if action == "swipe":
            x1, y1 = self._px(s["x1"], s["y1"])
            x2, y2 = self._px(s["x2"], s["y2"])
            ms = max(100, int(s.get("duration_ms", 300)))
            return adb.swipe(self.serial, x1, y1, x2, y2, ms, self.emulator)
        return False


def run_script_job(script_id: str = "", job_id: str | None = None,
                   serial: str = "", emulator: str = "") -> dict:
    if job_id:
        store.update_job(job_id, status="running", engine_used="script-replay")
    script = scripts.get_script(script_id) if script_id else None
    if not script:
        result = {"ok": False, "error": f"腳本不存在: {script_id}"}
    else:
        runner = ScriptRunner(script, serial=serial, emulator=emulator,
                              job_id=job_id)
        result = runner.run()
        result["script_id"] = script.get("id")
        result["script_name"] = script.get("name")
    if job_id:
        if result.get("ok"):
            summary = (f"[engine=script-replay] 腳本「{result.get('script_name','')}」"
                       f"執行完成：{result.get('steps_done')}/{result.get('total_steps')} 步，"
                       f"{result.get('elapsed')} 秒")
        else:
            done = result.get("steps_done")
            progress = (f"（完成 {done}/{result.get('total_steps')} 步）"
                        if done is not None else "")
            summary = f"[engine=script-replay] 失敗{progress}：{result.get('error','')}"
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            engine_used="script-replay",
            result=summary)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay a script via ADB (no AI)")
    ap.add_argument("--script", help="script id（data/scripts/<id>.yaml）")
    ap.add_argument("--job", help="job id（從 payload 取 script_id 並回寫狀態）")
    ap.add_argument("--serial", default="", help="覆寫目標裝置")
    ap.add_argument("--emulator", default="", help="覆寫模擬器類型")
    # spawn_runner 泛用參數（腳本重放不需要 AI，僅接受不使用）
    ap.add_argument("--engine", default="", help=argparse.SUPPRESS)
    ap.add_argument("--timeout", default="", help=argparse.SUPPRESS)
    ap.add_argument("--model", default="", help=argparse.SUPPRESS)
    ap.add_argument("--reasoning-effort", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    script_id = args.script or ""
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr)
            return 2
        script_id = script_id or job.get("payload", {}).get("script_id", "")

    result = run_script_job(script_id=script_id, job_id=args.job,
                            serial=args.serial, emulator=args.emulator)
    if result.get("ok"):
        print(f"完成：{result.get('steps_done')}/{result.get('total_steps')} 步，"
              f"{result.get('elapsed')} 秒")
        return 0
    print(f"失敗：{result.get('error','')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
