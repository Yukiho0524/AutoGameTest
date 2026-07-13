"""Script store: replayable game scripts generated from recordings.

A script is a YAML file under data/scripts/. Generation needs AI (Codex
annotates a deterministic skeleton built from taps.json); execution does NOT --
tools/run_script.py replays it with plain ADB commands.

Script schema:
    id: script_20260709_140000
    name: 口袋戰爭 每日流程
    source: rec_20260709_1010.mp4        # 來源錄影（相對 recordings 目錄或絕對路徑）
    game_id: game-id                     # 選填：綁定的遊戲
    game_name: 遊戲名稱                  # 選填：顯示用
    emulator: ldplayer
    serial: emulator-5554
    package: com.xxx                      # 選填：執行前啟動的 app
    description: ...                      # AI 產生的流程說明
    generated_by: codex | draft           # draft = AI 註解失敗、僅確定性骨架
    defaults:
      visual_timeout: 60       # tap_image/tap_scene 找模板最多等待秒數
      until_timeout: 120       # until / wait_scene 最多等待秒數
      stable_timeout: 45       # 有 wait_after 的操作後，最多等待畫面穩定秒數
      match_interval: 1.0      # 圖片比對輪詢間隔
      match_threshold: 0.72    # 圖片比對門檻，執行時限制在 0.6~0.8
    created: 2026-07-09 14:00:00
    steps:
      - action: tap | tap_image | tap_scene | long_press | swipe | wait | wait_scene | launch_app
        name: 點擊 出擊按鈕
        x: 0.5004        # tap/long_press：正規化座標 (0~1)
        y: 0.5007
        image: assets/foo/button.png  # tap_image/tap_scene/wait_scene：按鈕或畫面模板
        templates:                  # 可放多個候選模板，任一命中即可
          - image: assets/foo/button-a.png
            record_pos: [0.1, 0.2]  # Airtest-like：相對畫面中心的位置
            resolution: [1280, 720]
            target_pos: 5           # 九宮格點擊位置，5=中心
        threshold: 0.72  # 圖片比對門檻（建議 0.6~0.8）
        anchor: assets/foo/main.png   # 操作前需先看到的畫面/錨點
        scene: assets/foo/main.png    # 同 anchor，偏語意名稱
        until: assets/foo/done.png    # 操作後需等到的畫面/錨點
        duration_ms: 500  # long_press/swipe
        x1: ... y1: ... x2: ... y2: ...   # swipe
        seconds: 2.0      # wait
        wait_after: 1.5   # 每步之後等待秒數（重放節奏）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

try:
    import yaml
except ImportError:          # pragma: no cover - pyyaml is present on this box
    yaml = None

from . import store as _store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(ROOT, "data", "scripts")

_lock = threading.Lock()

VALID_ACTIONS = {
    "tap", "tap_image", "tap_scene", "long_press", "swipe", "wait",
    "wait_scene", "launch_app",
}
VISION_ACTIONS = {"tap_image", "tap_scene", "wait_scene"}
VISUAL_FIELDS = ("anchor", "scene", "until")
IMAGE_FIELDS = ("image", "template")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug or "script"


def yaml_available() -> bool:
    return yaml is not None


def list_scripts() -> list[dict]:
    """Metadata for every script (without full steps) sorted newest first."""
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    rows = []
    for fn in sorted(os.listdir(SCRIPTS_DIR)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        data = get_script(fn.rsplit(".", 1)[0])
        if not data:
            continue
        steps = data.get("steps") or []
        risk_count = sum(
            1 for step in steps
            if isinstance(step, dict) and step.get("risk"))
        vision_count = sum(
            1 for step in steps
            if isinstance(step, dict) and (
                step.get("action") in VISION_ACTIONS
                or any(step.get(k) for k in VISUAL_FIELDS)))
        rows.append({
            "id": data.get("id", fn.rsplit(".", 1)[0]),
            "name": data.get("name", fn),
            "source": data.get("source", ""),
            "game_id": data.get("game_id", ""),
            "game_name": data.get("game_name", ""),
            "emulator": data.get("emulator", ""),
            "serial": data.get("serial", ""),
            "package": data.get("package", ""),
            "description": data.get("description", ""),
            "generated_by": data.get("generated_by", ""),
            "created": data.get("created", ""),
            "n_steps": len(steps),
            "risk_count": risk_count,
            "vision_count": vision_count,
        })
    return sorted(rows, key=lambda r: r.get("created", ""), reverse=True)


def script_path(script_id: str) -> str:
    safe = os.path.basename(str(script_id))
    return os.path.join(SCRIPTS_DIR, f"{safe}.yaml")


def get_script(script_id: str) -> dict | None:
    path = script_path(script_id)
    if not os.path.isfile(path) or yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("id", script_id)
    return data


def get_script_text(script_id: str) -> str:
    path = script_path(script_id)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def validate_script(data: dict) -> str:
    """Return '' when the script is executable, else a reason."""
    if not isinstance(data, dict):
        return "腳本必須是物件"
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return "腳本沒有 steps"
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return f"step {i + 1} 不是物件"
        action = s.get("action")
        if action not in VALID_ACTIONS:
            return f"step {i + 1} 動作不支援: {action}"
        for field in VISUAL_FIELDS:
            err = _validate_visual_spec(s.get(field), i + 1, field)
            if err:
                return err
        if action in ("tap", "long_press"):
            for k in ("x", "y"):
                v = s.get(k)
                if not isinstance(v, (int, float)) or not (0 <= float(v) <= 1):
                    return f"step {i + 1} 缺少正規化座標 {k}（0~1）"
        if action == "tap_image":
            if not _has_image_spec(s):
                return f"step {i + 1} tap_image 需要 image/template"
        if action == "tap_scene":
            has_target = _has_image_spec(s)
            has_coord = all(isinstance(s.get(k), (int, float))
                            and 0 <= float(s.get(k)) <= 1 for k in ("x", "y"))
            if not has_target and not has_coord:
                return f"step {i + 1} tap_scene 需要 image/template 或 x/y"
            if not (s.get("anchor") or s.get("scene")):
                return f"step {i + 1} tap_scene 需要 anchor 或 scene 驗證"
        if action == "wait_scene":
            if not (_has_image_spec(s) or s.get("anchor") or s.get("scene")):
                return f"step {i + 1} wait_scene 需要 image/template/anchor/scene"
        if action == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                v = s.get(k)
                if not isinstance(v, (int, float)) or not (0 <= float(v) <= 1):
                    return f"step {i + 1} 缺少正規化座標 {k}（0~1）"
    return ""


def _has_image_spec(value: dict) -> bool:
    if any(isinstance(value.get(k), str) and value.get(k).strip()
           for k in IMAGE_FIELDS):
        return True
    templates = value.get("templates")
    return isinstance(templates, list) and any(
        isinstance(item, str) and item.strip()
        or isinstance(item, dict) and _has_image_spec(item)
        for item in templates)


def _validate_visual_spec(value, step_no: int, field: str) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, str):
        return "" if value.strip() else f"step {step_no} {field} 是空字串"
    if isinstance(value, list):
        for j, item in enumerate(value, 1):
            err = _validate_visual_spec(item, step_no, f"{field}[{j}]")
            if err:
                return err
        return ""
    if not isinstance(value, dict):
        return f"step {step_no} {field} 必須是字串、物件或列表"
    if _has_image_spec(value):
        return ""
    if isinstance(value.get("templates"), list):
        return _validate_visual_spec(value["templates"], step_no, field)
    nested = value.get("all") or value.get("any")
    if isinstance(nested, list):
        return _validate_visual_spec(nested, step_no, field)
    return f"step {step_no} {field} 需要 image/template"


def save_script(data: dict, script_id: str | None = None) -> dict:
    """Persist a script dict as YAML. Returns the saved dict (with id)."""
    if yaml is None:
        raise RuntimeError("需要 PyYAML 才能儲存腳本")
    with _lock:
        os.makedirs(SCRIPTS_DIR, exist_ok=True)
        sid = script_id or data.get("id")
        if not sid:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            sid = f"script_{stamp}"
            n = 2
            while os.path.isfile(script_path(sid)):
                sid = f"script_{stamp}_{n}"; n += 1
        data["id"] = sid
        data.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
        path = script_path(sid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                           default_flow_style=False)
        os.replace(tmp, path)
        return data


def save_script_text(text: str, script_id: str | None = None) -> dict:
    """Parse YAML text, validate, and save. Raises ValueError on bad input."""
    if yaml is None:
        raise RuntimeError("需要 PyYAML 才能儲存腳本")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失敗: {e}") from e
    err = validate_script(data if isinstance(data, dict) else {})
    if err:
        raise ValueError(err)
    return save_script(data, script_id=script_id)


def delete_script(script_id: str) -> bool:
    with _lock:
        path = script_path(script_id)
        try:
            os.remove(path)
            return True
        except OSError:
            return False


# ---------------- recordings discovery ----------------

def _recording_dirs() -> list[str]:
    from . import recorder
    dirs = [recorder.DEFAULT_SAVE_DIR]
    saved = _store.get_settings().get("recording_dir", "")
    if saved:
        dirs.append(recorder.resolve_save_dir(saved))
    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def taps_json_for(source_path: str) -> str:
    """Path of the taps.json belonging to a recording ('' if missing)."""
    if os.path.isdir(source_path):
        p = os.path.join(source_path, "taps.json")
    else:
        p = source_path + ".taps.json"
    return p if os.path.isfile(p) else ""


def load_taps(source_path: str) -> list[dict]:
    tj = taps_json_for(source_path)
    if not tj:
        return []
    try:
        with open(tj, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def list_recordings() -> list[dict]:
    """All recordings in known folders, newest first, with taps availability."""
    rows = []
    for d in _recording_dirs():
        for fn in os.listdir(d):
            full = os.path.join(d, fn)
            if fn.startswith("rec_") and fn.endswith(".mp4") and os.path.isfile(full):
                rows.append(_recording_row(full, fn))
            elif (fn.startswith("rec_") and os.path.isdir(full)
                  and os.path.isfile(os.path.join(full, "session.json"))):
                rows.append(_recording_row(full, fn + "/（多段）"))
    return sorted(rows, key=lambda r: r.get("mtime", 0), reverse=True)


def _recording_row(full: str, label: str) -> dict:
    taps = load_taps(full)
    try:
        mtime = os.path.getmtime(full)
    except OSError:
        mtime = 0
    return {
        "path": full,
        "label": label,
        "has_taps": bool(taps),
        "n_taps": len(taps),
        "mtime": mtime,
        "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        if mtime else "",
    }


# ---------------- deterministic skeleton (from taps.json) ----------------

def build_skeleton(source_path: str, name: str = "",
                   emulator: str = "", serial: str = "",
                   package: str = "",
                   game_id: str = "", game_name: str = "") -> dict:
    """Build a runnable draft script straight from taps.json (no AI).

    Timing: each step waits the real gap observed between recorded touches
    (with a small buffer for loading) so replay follows the demonstrated rhythm.
    """
    taps = load_taps(source_path)
    if not taps:
        raise ValueError("此錄影沒有 taps.json（錄影時需在模擬器視窗或操控分頁點擊）")
    steps: list[dict] = []
    if package:
        steps.append({"action": "launch_app", "name": f"啟動 {package}",
                      "wait_after": 8.0})
    prev_t = None
    for i, tp in enumerate(taps):
        gap = 0.0 if prev_t is None else max(0.0, float(tp["t"]) - prev_t)
        prev_t = float(tp["t"])
        if gap > 1.2 and steps:
            buffer = 2.0 if gap >= 8.0 else 0.0
            steps[-1]["wait_after"] = round(min(gap + buffer, 90.0), 1)
        kind = tp.get("kind", "tap")
        if kind == "swipe":
            steps.append({
                "action": "swipe",
                "name": f"滑動 t={tp['t']:.1f}s",
                "x1": tp["nx"], "y1": tp["ny"],
                "x2": tp.get("end_nx", tp["nx"]),
                "y2": tp.get("end_ny", tp["ny"]),
                "duration_ms": max(200, int(tp.get("duration_ms", 300))),
                "wait_after": 1.0,
            })
        elif kind == "long_press":
            steps.append({
                "action": "long_press",
                "name": f"長壓 t={tp['t']:.1f}s",
                "x": tp["nx"], "y": tp["ny"],
                "duration_ms": max(400, int(tp.get("duration_ms", 500))),
                "wait_after": 1.0,
            })
        else:
            steps.append({
                "action": "tap",
                "name": f"點擊 t={tp['t']:.1f}s",
                "x": tp["nx"], "y": tp["ny"],
                "wait_after": 1.0,
            })
    base = os.path.basename(source_path)
    return {
        "name": name or f"腳本 {base}",
        "source": source_path,
        "game_id": game_id,
        "game_name": game_name,
        "emulator": emulator or "ldplayer",
        "serial": serial or "emulator-5554",
        "package": package,
        "description": f"由錄影 {base} 的 {len(taps)} 個實測觸控生成",
        "generated_by": "draft",
        "defaults": {
            "visual_timeout": 60,
            "until_timeout": 120,
            "stable_timeout": 45,
            "match_interval": 1.0,
            "match_threshold": 0.72,
        },
        "steps": steps,
    }
