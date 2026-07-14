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
DEFAULT_VISUAL_TIMEOUT = 60.0
DEFAULT_UNTIL_TIMEOUT = 120.0
DEFAULT_MATCH_INTERVAL = 1.0
DEFAULT_STABLE_TIMEOUT = 45.0
DEFAULT_MATCH_THRESHOLD = 0.72
MIN_MATCH_THRESHOLD = 0.60
MAX_MATCH_THRESHOLD = 0.80


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


def _safe_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
    return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
        self.defaults = script.get("defaults") if isinstance(
            script.get("defaults"), dict) else {}
        self.last_step_skipped = False
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

    def _default_seconds(self, key: str, fallback: float) -> float:
        value = self.defaults.get(key)
        seconds = _safe_float(value, fallback)
        return max(0.0, seconds)

    def _match_threshold(self, spec: dict) -> float:
        default = _safe_float(
            self.defaults.get("match_threshold"), DEFAULT_MATCH_THRESHOLD)
        threshold = _safe_float(spec.get("threshold"), default)
        return _clamp(threshold, MIN_MATCH_THRESHOLD, MAX_MATCH_THRESHOLD)

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

    def _template_candidates(self, spec: dict) -> list[dict]:
        if not isinstance(spec, dict):
            return []
        templates = spec.get("templates")
        if isinstance(templates, list):
            out = []
            base = {
                k: v for k, v in spec.items()
                if k not in ("templates", "image", "template")
            }
            for item in templates:
                if isinstance(item, str):
                    cur = dict(base)
                    cur["image"] = item
                    out.append(cur)
                elif isinstance(item, dict):
                    cur = dict(base)
                    cur.update(item)
                    out.append(cur)
            if out:
                return out
        image_value = spec.get("image") or spec.get("template")
        if isinstance(image_value, list):
            out = []
            base = dict(spec)
            base.pop("image", None)
            base.pop("template", None)
            for item in image_value:
                cur = dict(base)
                cur["image"] = item
                out.append(cur)
            return out
        return [spec] if self._image_spec_path(spec) else []

    def _candidate_match(self, png: bytes, candidate: dict) -> dict:
        path = self._image_spec_path(candidate)
        if not path:
            return {"found": False, "score": 0.0,
                    "error": "missing image/template"}
        threshold = self._match_threshold(candidate)
        match = image_match.match_template(
            png, path, threshold=threshold,
            region=candidate.get("region"),
            record_pos=candidate.get("record_pos"),
            resolution=candidate.get("resolution"),
            rgb=_safe_bool(candidate.get("rgb"), False),
            scan_step=_safe_int(candidate.get("scan_step"), 0) or None,
            max_points=_safe_int(candidate.get("max_points"), 121),
            allow_full_search=self._optional_bool(
                candidate.get("allow_full_search")))
        for key in ("target_pos", "tap_offset", "offset"):
            if key in candidate:
                match[key] = candidate[key]
        return match

    def _wait_visual(self, value, label: str,
                     default_timeout: float | None = None) -> bool:
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
        candidates = self._template_candidates(spec)
        if not candidates:
            self._progress(f"{label} 缺少 image/template")
            return False
        if default_timeout is None:
            default_timeout = self._default_seconds(
                "visual_timeout", DEFAULT_VISUAL_TIMEOUT)
        timeout = _safe_float(spec.get("timeout"), default_timeout)
        threshold = self._match_threshold(spec)
        interval = max(0.2, _safe_float(
            spec.get("interval"),
            self._default_seconds("match_interval", DEFAULT_MATCH_INTERVAL)))
        deadline = time.time() + max(0.0, timeout)
        last = {"score": 0.0, "error": ""}
        started = time.time()
        next_progress = started + 10.0
        label_name = self._candidate_label(candidates)
        if timeout >= 10:
            self._progress(
                f"{label} 等待畫面 {label_name}，最多 {timeout:g} 秒")
        while True:
            png = self._grab_screen()
            if png:
                last = self._best_candidate_match(png, candidates)
                if last.get("found"):
                    waited = time.time() - started
                    self._progress(self._match_message(
                        label, last, label_name, waited=waited))
                    return True
            if timeout <= 0 or time.time() >= deadline:
                break
            if time.time() >= next_progress:
                self._progress(
                    f"{label} 還在等待 {label_name}"
                    f"（score={last.get('score', 0.0)}）")
                next_progress = time.time() + 10.0
            time.sleep(interval)
        base = os.path.basename(last.get("template") or label_name)
        score = last.get("score", 0.0)
        err = f" ({last.get('error')})" if last.get("error") else ""
        self._progress(
            f"{label} 驗證失敗：找不到 {base} score={score}/{threshold}{err}")
        return False

    def _locate_template(self, step: dict) -> dict:
        candidates = self._template_candidates(step)
        if not candidates:
            return {"found": False, "score": 0.0,
                    "error": "missing image/template"}
        timeout = _safe_float(
            step.get("timeout"),
            self._default_seconds("visual_timeout", DEFAULT_VISUAL_TIMEOUT))
        threshold = self._match_threshold(step)
        interval = max(0.2, _safe_float(
            step.get("interval"),
            self._default_seconds("match_interval", DEFAULT_MATCH_INTERVAL)))
        deadline = time.time() + max(0.0, timeout)
        last = {"found": False, "score": 0.0, "error": ""}
        started = time.time()
        next_progress = started + 10.0
        label_name = self._candidate_label(candidates)
        if timeout >= 10:
            self._progress(
                f"{step.get('action')} 等待模板 {label_name}，"
                f"最多 {timeout:g} 秒")
        while True:
            png = self._grab_screen()
            if png:
                last = self._best_candidate_match(png, candidates)
                if last.get("found"):
                    waited = time.time() - started
                    self._progress(self._match_message(
                        str(step.get("action") or "match"),
                        last, label_name, waited=waited))
                    return last
            if timeout <= 0 or time.time() >= deadline:
                break
            if time.time() >= next_progress:
                self._progress(
                    f"{step.get('action')} 還在等待 {label_name}"
                    f"（score={last.get('score', 0.0)}）")
                next_progress = time.time() + 10.0
            time.sleep(interval)
        return last

    def _candidate_label(self, candidates: list[dict]) -> str:
        names = [
            os.path.basename(self._image_spec_path(candidate))
            for candidate in candidates[:3]
        ]
        names = [name for name in names if name]
        if len(candidates) > 3:
            names.append(f"+{len(candidates) - 3}")
        return ", ".join(names) or "template"

    def _optional_bool(self, value) -> bool | None:
        if value is None:
            return None
        return _safe_bool(value, False)

    def _match_message(self, label: str, match: dict, fallback: str,
                       waited: float | None = None) -> str:
        name = os.path.basename(match.get("template") or fallback)
        parts = [f"{label} 已找到 {name}"]
        if waited is not None:
            parts.append(f"{waited:.1f}s")
        parts.append(f"score={match.get('score')}")
        if match.get("px") is not None and match.get("py") is not None:
            parts.append(f"pos=({match.get('px')},{match.get('py')})")
        if match.get("search_mode"):
            parts.append(f"mode={match.get('search_mode')}")
        return "（".join([parts[0], "，".join(parts[1:]) + "）"]) if len(parts) > 1 else parts[0]

    def _best_candidate_match(self, png: bytes, candidates: list[dict]) -> dict:
        best = {"found": False, "score": 0.0, "error": ""}
        for candidate in candidates:
            match = self._candidate_match(png, candidate)
            if match.get("found"):
                return match
            if match.get("score", 0.0) > best.get("score", 0.0):
                best = match
            elif match.get("error") and not best.get("error"):
                best = match
        return best

    def _precheck_visuals(self, step: dict) -> bool:
        timeout = _safe_float(
            step.get("anchor_timeout"),
            self._default_seconds("visual_timeout", DEFAULT_VISUAL_TIMEOUT))
        if not self._wait_visual(step.get("anchor"), "anchor", timeout):
            return self._skip_on_timeout(step, "anchor timeout")
        if not self._wait_visual(step.get("scene"), "scene", timeout):
            return self._skip_on_timeout(step, "scene timeout")
        return True

    def _verify_until(self, step: dict) -> bool:
        until = step.get("until")
        if until in (None, "", []):
            return True
        timeout = _safe_float(
            step.get("until_timeout"),
            self._default_seconds("until_timeout", DEFAULT_UNTIL_TIMEOUT))
        if self._wait_visual(until, "until", timeout):
            return True
        return self._skip_on_timeout(step, "until timeout")

    def _on_timeout_mode(self, step: dict) -> str:
        return str(step.get("on_timeout", "") or "").strip().lower()

    def _skip_on_timeout(self, step: dict, reason: str) -> bool:
        if self._on_timeout_mode(step) not in ("skip", "continue", "next"):
            return False
        self.last_step_skipped = True
        self._progress(f"{reason}，on_timeout=skip，略過本步並繼續下一步")
        return True

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
        if dx == 0.0 and dy == 0.0:
            pos = _safe_int(match.get("target_pos", step.get("target_pos", 5)), 5)
            if 1 <= pos <= 9 and all(k in match for k in ("left", "top", "right", "bottom")):
                col = (pos - 1) % 3
                row = (pos - 1) // 3
                px = int(round(match["left"] + (col + 0.5) * (match["right"] - match["left"]) / 3))
                py = int(round(match["top"] + (row + 0.5) * (match["bottom"] - match["top"]) / 3))
        elif abs(dx) <= 1 and abs(dy) <= 1:
            px += int(round(dx * int(match.get("template_width", 1))))
            py += int(round(dy * int(match.get("template_height", 1))))
        else:
            px += int(round(dx))
            py += int(round(dy))
        px = max(0, min(self.width - 1, px))
        py = max(0, min(self.height - 1, py))
        self._progress(f"執行點擊：({px},{py})")
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
            self.last_step_skipped = False
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
            if wait_after > 0 and not self.last_step_skipped:
                time.sleep(min(wait_after, 120))
                stable_default = self._default_seconds(
                    "stable_timeout", DEFAULT_STABLE_TIMEOUT)
                if action != "wait" and s.get("stable_timeout") is not None:
                    extra = _safe_float(s.get("stable_timeout"), stable_default)
                elif action != "wait" and wait_after >= 1:
                    extra = min(stable_default, max(8.0, wait_after * 0.75))
                else:
                    extra = 0.0
                if extra > 0:
                    if extra >= 10:
                        self._progress(f"等待載入/轉場穩定，最多 {extra:g} 秒")
                    self._wait_for_screen_stable(extra)
            self._screenshot(f"step_{i:02d}")
        elapsed = round(time.time() - started, 1)
        return {"ok": True, "steps_done": self.steps_done,
                "total_steps": total, "elapsed": elapsed}

    def _exec_step(self, s: dict) -> bool:
        action = s.get("action")
        if not self._precheck_visuals(s):
            return False
        if self.last_step_skipped:
            return True
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
                    f"{match.get('threshold', self._match_threshold(s))}"
                    f"{' ' + match.get('error', '') if match.get('error') else ''}")
                return self._skip_on_timeout(s, "tap_image timeout")
            ok = self._tap_from_match(match, s)
        elif action == "tap_scene":
            if s.get("image") or s.get("template"):
                match = self._locate_template(s)
                if not match.get("found"):
                    self._progress(
                        f"tap_scene 找不到模板：score={match.get('score', 0.0)}/"
                        f"{match.get('threshold', self._match_threshold(s))}"
                        f"{' ' + match.get('error', '') if match.get('error') else ''}")
                    return self._skip_on_timeout(s, "tap_scene timeout")
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
            if not ok:
                return self._skip_on_timeout(s, "wait_scene timeout")
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


def _resolve_replay_target(script: dict, serial: str = "",
                           emulator: str = "") -> tuple[str, str]:
    """Resolve the current machine's replay target for a saved script.

    Scripts are portable assets. The serial saved at generation time may belong
    to another PC (for example 127.0.0.1:5555 vs emulator-5554), so when the
    script is bound to a game we prefer the current game launch settings.
    Explicit CLI/API overrides still win.
    """
    explicit_emulator = str(emulator or "").strip()
    explicit_serial = str(serial or "").strip()
    game_launch = {}
    game_id = str(script.get("game_id") or "").strip()
    if game_id:
        game = store.get_game(game_id) or {}
        if isinstance(game.get("launch"), dict):
            game_launch = game["launch"]

    resolved_emulator = adb.normalize_emulator(
        explicit_emulator
        or game_launch.get("emulator")
        or script.get("emulator")
        or "ldplayer")
    if explicit_serial:
        return explicit_serial, resolved_emulator

    if game_launch:
        try:
            instance = int(game_launch.get("instance", 0) or 0)
        except (TypeError, ValueError):
            instance = 0
        configured_serial = str(game_launch.get("serial", "") or "").strip()
        return configured_serial or adb.serial_for(instance, resolved_emulator), resolved_emulator

    saved_serial = str(script.get("serial", "") or "").strip()
    return saved_serial or adb.serial_for(0, resolved_emulator), resolved_emulator


def run_script_job(script_id: str = "", job_id: str | None = None,
                   serial: str = "", emulator: str = "",
                   allow_risk: bool = False) -> dict:
    if job_id:
        store.update_job(job_id, status="running", engine_used="script-replay")
    script = scripts.get_script(script_id) if script_id else None
    if not script:
        result = {"ok": False, "error": f"腳本不存在: {script_id}"}
    else:
        resolved_serial, resolved_emulator = _resolve_replay_target(
            script, serial=serial, emulator=emulator)
        runner = ScriptRunner(script, serial=resolved_serial,
                              emulator=resolved_emulator,
                              job_id=job_id, allow_risk=allow_risk)
        result = runner.run()
        result["script_id"] = script.get("id")
        result["script_name"] = script.get("name")
        result["serial"] = resolved_serial
        result["emulator"] = resolved_emulator
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
        args.serial = args.serial or str(payload.get("serial", "") or "")
        args.emulator = args.emulator or str(payload.get("emulator", "") or "")

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
