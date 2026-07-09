"""Replay a generated script with plain ADB — no AI involved.

This is the execution half of the 腳本 feature: generation needs Codex, but
replay is deterministic ADB playback. Scripts can use either fixed normalized
coordinates or template-matched image taps with scene/until verification. Runs
as a job runner (same spawn contract as run_agent.py) or standalone via --script.

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

from core import store, adb, scripts, fast_agent, image_match  # noqa: E402

ARTIFACTS_DIR = os.path.join(ROOT, "data", "artifacts")
STABLE_AHASH_DISTANCE = 4
STABLE_CHECK_INTERVAL = 1.5
DEFAULT_VISUAL_TIMEOUT = 12.0
DEFAULT_UNTIL_TIMEOUT = 30.0
DEFAULT_MATCH_INTERVAL = 0.7


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height from a PNG's IHDR (stdlib-only; no cv2 needed)."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    return 0, 0


def _ahash_distance(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ScriptRunner:
    def __init__(self, script: dict, serial: str = "", emulator: str = "",
                 job_id: str | None = None, allow_risk: bool = False):
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
        self.allow_risk = bool(allow_risk)
        if job_id:
            self.art_dir = os.path.join(ARTIFACTS_DIR, job_id)
            os.makedirs(self.art_dir, exist_ok=True)

    # ---- helpers ----
    def _progress(self, text: str) -> None:
        print(text, flush=True)
        if self.job_id:
            store.update_job(self.job_id, progress=text)

    def _grab_screen(self) -> bytes | None:
        png = adb.screenshot(self.serial, self.emulator)
        if png and not self.width:
            self.width, self.height = _png_size(png)
        return png

    def _screenshot(self, tag: str) -> None:
        if not self.art_dir:
            return
        png = self._grab_screen()
        if not png:
            return
        path = os.path.join(self.art_dir, f"{tag}.png")
        try:
            with open(path, "wb") as f:
                f.write(png)
            self.shots.append(path)
        except OSError:
            pass

    def _resolve_size(self) -> bool:
        png = self._grab_screen()
        if not png:
            return False
        self.width, self.height = _png_size(png)
        return self.width > 0 and self.height > 0

    def _screenshot_signature(self) -> dict:
        png = adb.screenshot(self.serial, self.emulator)
        if not png:
            return {}
        return fast_agent.screen_signature(png)

    def _wait_for_screen_stable(self, max_seconds: float) -> None:
        """Wait through variable loading/transition screens before next input."""
        max_seconds = max(0.0, float(max_seconds or 0))
        if max_seconds <= 0:
            return
        prev = self._screenshot_signature().get("ahash", "")
        if not prev:
            return
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            time.sleep(STABLE_CHECK_INTERVAL)
            cur = self._screenshot_signature().get("ahash", "")
            if not cur:
                return
            distance = _ahash_distance(prev, cur)
            if distance <= STABLE_AHASH_DISTANCE:
                return
            prev = cur

    def _px(self, nx, ny) -> tuple[int, int]:
        x = int(round(float(nx) * (self.width - 1)))
        y = int(round(float(ny) * (self.height - 1)))
        return max(0, min(self.width - 1, x)), max(0, min(self.height - 1, y))

    def _script_dir(self) -> str:
        script_id = str(self.script.get("id") or "").strip()
        if script_id:
            return os.path.dirname(scripts.script_path(script_id))
        return scripts.SCRIPTS_DIR

    def _resolve_asset(self, value: str) -> str:
        raw = str(value or "").strip().strip("\"'")
        if not raw:
            return ""
        raw = raw.replace("/", os.sep)
        if os.path.isabs(raw):
            return raw
        candidates = [
            os.path.join(self._script_dir(), raw),
            os.path.join(ROOT, raw),
            os.path.join(scripts.SCRIPTS_DIR, raw),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
        return candidates[0]

    def _image_spec_path(self, spec: dict) -> str:
        return self._resolve_asset(spec.get("image") or spec.get("template") or "")

    def _as_visual_spec(self, value) -> dict:
        if isinstance(value, str):
            return {"image": value}
        return value if isinstance(value, dict) else {}

    def _wait_visual(self, value, label: str,
                     default_timeout: float = DEFAULT_VISUAL_TIMEOUT) -> bool:
        if value in (None, "", []):
            return True
        if isinstance(value, list):
            return all(self._wait_visual(item, label, default_timeout)
                       for item in value)
        if isinstance(value, dict) and isinstance(value.get("all"), list):
            return all(self._wait_visual(item, label, default_timeout)
                       for item in value["all"])
        if isinstance(value, dict) and isinstance(value.get("any"), list):
            return any(self._wait_visual(item, label, default_timeout)
                       for item in value["any"])

        spec = self._as_visual_spec(value)
        path = self._image_spec_path(spec)
        if not path:
            self._progress(f"{label} 缺少 image/template")
            return False
        timeout = _safe_float(spec.get("timeout"), default_timeout)
        threshold = _safe_float(
            spec.get("threshold"), image_match.DEFAULT_THRESHOLD)
        interval = max(0.2, _safe_float(
            spec.get("interval"), DEFAULT_MATCH_INTERVAL))
        deadline = time.time() + max(0.0, timeout)
        last = {"score": 0.0, "error": ""}
        while True:
            png = self._grab_screen()
            if png:
                last = image_match.match_template(
                    png, path, threshold=threshold,
                    region=spec.get("region"),
                    scan_step=_safe_int(spec.get("scan_step"), 0) or None,
                    max_points=_safe_int(spec.get("max_points"), 121))
                if last.get("found"):
                    return True
            if timeout <= 0 or time.time() >= deadline:
                break
            time.sleep(interval)
        base = os.path.basename(path)
        score = last.get("score", 0.0)
        err = f" ({last.get('error')})" if last.get("error") else ""
        self._progress(
            f"{label} 驗證失敗：找不到 {base} score={score}/{threshold}{err}")
        return False

    def _locate_template(self, step: dict) -> dict:
        path = self._image_spec_path(step)
        if not path:
            return {"found": False, "score": 0.0,
                    "error": "missing image/template"}
        timeout = _safe_float(step.get("timeout"), DEFAULT_VISUAL_TIMEOUT)
        threshold = _safe_float(
            step.get("threshold"), image_match.DEFAULT_THRESHOLD)
        interval = max(0.2, _safe_float(
            step.get("interval"), DEFAULT_MATCH_INTERVAL))
        deadline = time.time() + max(0.0, timeout)
        last = {"found": False, "score": 0.0, "error": ""}
        while True:
            png = self._grab_screen()
            if png:
                last = image_match.match_template(
                    png, path, threshold=threshold,
                    region=step.get("region"),
                    scan_step=_safe_int(step.get("scan_step"), 0) or None,
                    max_points=_safe_int(step.get("max_points"), 121))
                if last.get("found"):
                    return last
            if timeout <= 0 or time.time() >= deadline:
                break
            time.sleep(interval)
        return last

    def _precheck_visuals(self, step: dict) -> bool:
        timeout = _safe_float(step.get("anchor_timeout"), DEFAULT_VISUAL_TIMEOUT)
        if not self._wait_visual(step.get("anchor"), "anchor", timeout):
            return False
        if not self._wait_visual(step.get("scene"), "scene", timeout):
            return False
        return True

    def _verify_until(self, step: dict) -> bool:
        until = step.get("until")
        if until in (None, "", []):
            return True
        timeout = _safe_float(step.get("until_timeout"), DEFAULT_UNTIL_TIMEOUT)
        return self._wait_visual(until, "until", timeout)

    def _tap_from_match(self, match: dict, step: dict) -> bool:
        px = int(match.get("px", 0))
        py = int(match.get("py", 0))
        offset = step.get("tap_offset") or step.get("offset")
        if isinstance(offset, dict):
            dx = _safe_float(offset.get("x", offset.get("dx")), 0.0)
            dy = _safe_float(offset.get("y", offset.get("dy")), 0.0)
        elif isinstance(offset, (list, tuple)) and len(offset) == 2:
            dx, dy = _safe_float(offset[0]), _safe_float(offset[1])
        else:
            dx = dy = 0.0
        if abs(dx) <= 1 and abs(dy) <= 1:
            px += int(round(dx * int(match.get("template_width", 1))))
            py += int(round(dy * int(match.get("template_height", 1))))
        else:
            px += int(round(dx))
            py += int(round(dy))
        px = max(0, min(self.width - 1, px))
        py = max(0, min(self.height - 1, py))
        return adb.tap(self.serial, px, py, self.emulator)

    # ---- run ----
    def run(self) -> dict:
        err = scripts.validate_script(self.script)
        if err:
            return {"ok": False, "error": f"腳本無效：{err}"}
        risky_steps = [
            i + 1 for i, step in enumerate(self.script.get("steps") or [])
            if isinstance(step, dict) and step.get("risk")]
        if risky_steps and not self.allow_risk:
            return {
                "ok": False,
                "error": (
                    "腳本包含高風險步驟（可能是抽卡、消耗資源或購買）。"
                    f"需明確確認後才能執行；風險步驟：{risky_steps}")
            }
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
            if not ok:
                self._screenshot(f"step_{i:02d}")
                elapsed = round(time.time() - started, 1)
                return {"ok": False, "steps_done": self.steps_done,
                        "total_steps": total, "elapsed": elapsed,
                        "error": f"step {i}（{name}）執行失敗（操作或畫面驗證未通過）"}
            self.steps_done = i
            wait_after = float(s.get("wait_after", 0) or 0)
            if wait_after > 0:
                time.sleep(min(wait_after, 120))
                if action != "wait" and wait_after >= 5:
                    extra = min(45.0, max(8.0, wait_after * 0.5))
                    self._wait_for_screen_stable(extra)
            self._screenshot(f"step_{i:02d}")
        elapsed = round(time.time() - started, 1)
        return {"ok": True, "steps_done": self.steps_done,
                "total_steps": total, "elapsed": elapsed}

    def _exec_step(self, s: dict) -> bool:
        action = s.get("action")
        if not self._precheck_visuals(s):
            return False
        ok = False
        if action == "wait":
            time.sleep(min(float(s.get("seconds", 1) or 1), 300))
            ok = True
        elif action == "launch_app":
            package = s.get("package") or self.script.get("package")
            if not package:
                ok = True   # nothing to launch is not a failure
            else:
                ok = adb.launch_app(self.serial, package, self.emulator)
        elif action == "tap":
            x, y = self._px(s["x"], s["y"])
            ok = adb.tap(self.serial, x, y, self.emulator)
        elif action == "tap_image":
            match = self._locate_template(s)
            if not match.get("found"):
                self._progress(
                    f"tap_image 找不到模板：score={match.get('score', 0.0)}/"
                    f"{match.get('threshold', s.get('threshold', image_match.DEFAULT_THRESHOLD))}"
                    f"{' ' + match.get('error', '') if match.get('error') else ''}")
                return False
            ok = self._tap_from_match(match, s)
        elif action == "tap_scene":
            if s.get("image") or s.get("template"):
                match = self._locate_template(s)
                if not match.get("found"):
                    self._progress(
                        f"tap_scene 找不到模板：score={match.get('score', 0.0)}/"
                        f"{match.get('threshold', s.get('threshold', image_match.DEFAULT_THRESHOLD))}"
                        f"{' ' + match.get('error', '') if match.get('error') else ''}")
                    return False
                ok = self._tap_from_match(match, s)
            else:
                x, y = self._px(s["x"], s["y"])
                ok = adb.tap(self.serial, x, y, self.emulator)
        elif action == "wait_scene":
            if s.get("image") or s.get("template"):
                ok = self._wait_visual(s, "wait_scene", _safe_float(
                    s.get("timeout"), DEFAULT_UNTIL_TIMEOUT))
            else:
                ok = self._wait_visual(
                    s.get("scene") or s.get("anchor"), "wait_scene",
                    _safe_float(s.get("timeout"), DEFAULT_UNTIL_TIMEOUT))
        elif action == "long_press":
            x, y = self._px(s["x"], s["y"])
            ms = max(400, int(s.get("duration_ms", 600)))
            # same-point swipe with duration = long press
            ok = adb.swipe(self.serial, x, y, x, y, ms, self.emulator)
        elif action == "swipe":
            x1, y1 = self._px(s["x1"], s["y1"])
            x2, y2 = self._px(s["x2"], s["y2"])
            ms = max(100, int(s.get("duration_ms", 300)))
            ok = adb.swipe(self.serial, x1, y1, x2, y2, ms, self.emulator)
        if not ok:
            return False
        return self._verify_until(s)


def run_script_job(script_id: str = "", job_id: str | None = None,
                   serial: str = "", emulator: str = "",
                   allow_risk: bool = False) -> dict:
    if job_id:
        store.update_job(job_id, status="running", engine_used="script-replay")
    script = scripts.get_script(script_id) if script_id else None
    if not script:
        result = {"ok": False, "error": f"腳本不存在: {script_id}"}
    else:
        runner = ScriptRunner(script, serial=serial, emulator=emulator,
                              job_id=job_id, allow_risk=allow_risk)
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
    ap.add_argument("--allow-risk", action="store_true",
                    help="允許執行標記為 risk 的腳本步驟")
    # spawn_runner 泛用參數（腳本重放不需要 AI，僅接受不使用）
    ap.add_argument("--engine", default="", help=argparse.SUPPRESS)
    ap.add_argument("--timeout", default="", help=argparse.SUPPRESS)
    ap.add_argument("--model", default="", help=argparse.SUPPRESS)
    ap.add_argument("--reasoning-effort", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    script_id = args.script or ""
    allow_risk = bool(args.allow_risk)
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr)
            return 2
        payload = job.get("payload", {})
        script_id = script_id or payload.get("script_id", "")
        allow_risk = bool(payload.get("allow_risk"))

    result = run_script_job(script_id=script_id, job_id=args.job,
                            serial=args.serial, emulator=args.emulator,
                            allow_risk=allow_risk)
    if result.get("ok"):
        print(f"完成：{result.get('steps_done')}/{result.get('total_steps')} 步，"
              f"{result.get('elapsed')} 秒")
        return 0
    print(f"失敗：{result.get('error','')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
