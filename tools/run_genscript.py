"""Generate a replayable script from a recording — computed entirely by Codex.

Codex receives the raw taps.json (exact getevent touch data), a keyframe per
touch, and the script schema/rules, then produces the COMPLETE script YAML:
which touches become steps, waits, names, risk flags — all AI-computed.

Python's role is reduced to safety validation (action whitelist, normalized
coordinate range, forced metadata) and a fallback: if Codex fails or returns
invalid YAML, a deterministic skeleton built from taps.json is saved as a
draft so the recording is never wasted.

Usage:
    python tools/run_genscript.py --job <job_id>
    python tools/run_genscript.py --source data/recordings/rec_xxx.mp4 --name 我的流程
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import store, scripts  # noqa: E402
import ai_runner  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_ASSETS_DIR = os.path.join(ROOT, "data", "scripts", "assets")
DEFAULT_SCRIPT_DEFAULTS = {
    "visual_timeout": 60,
    "until_timeout": 120,
    "stable_timeout": 45,
    "match_interval": 1.0,
    "match_threshold": 0.72,
}
MIN_MATCH_THRESHOLD = 0.60
MAX_MATCH_THRESHOLD = 0.80

# how far before the recorded tap time to grab the frame (screenrecord lag)
FRAME_LAG = 0.45


def extract_keyframes(source: str, taps: list[dict], out_dir: str) -> list[str]:
    """Grab one frame per touch (just before the press). Returns saved paths.
    Needs cv2; returns [] when unavailable so generation still works."""
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return []
    parts = _video_parts(source)
    if not parts:
        return []
    metas = []
    for p in parts:
        cap = cv2.VideoCapture(p)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()
        metas.append((p, fps, n / fps if fps else 0.0))
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for i, tp in enumerate(taps):
        t = max(0.0, float(tp.get("t", 0)) - FRAME_LAG)
        frame = _frame_at(cv2, metas, t)
        if frame is None:
            continue
        path = os.path.join(out_dir, f"tap{i:02d}.png")
        if cv2.imwrite(path, frame):
            saved.append(path)
    return saved


def extract_touch_templates(frames: list[str], taps: list[dict],
                            out_dir: str) -> list[dict]:
    """Crop a small visual target around each recorded tap for tap_image."""
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return []
    if not frames:
        return []
    os.makedirs(out_dir, exist_ok=True)
    saved: list[dict] = []
    for frame_path in frames:
        idx = _tap_index_from_frame(frame_path)
        if idx < 0 or idx >= len(taps):
            continue
        tap = taps[idx]
        if tap.get("kind") == "swipe":
            continue
        frame = cv2.imread(frame_path)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        try:
            cx = int(float(tap.get("nx")) * w)
            cy = int(float(tap.get("ny")) * h)
        except (TypeError, ValueError):
            continue
        half_w = max(36, min(180, int(w * 0.09)))
        half_h = max(28, min(120, int(h * 0.08)))
        x1, x2 = max(0, cx - half_w), min(w, cx + half_w)
        y1, y2 = max(0, cy - half_h), min(h, cy + half_h)
        if x2 - x1 < 12 or y2 - y1 < 12:
            continue
        path = os.path.join(out_dir, f"tap{idx:02d}_template.png")
        if cv2.imwrite(path, frame[y1:y2, x1:x2]):
            saved.append({
                "tap_index": idx,
                "image": _relative_path(path),
                "record_pos": [
                    round(float(tap.get("nx", 0.5)) - 0.5, 4),
                    round(float(tap.get("ny", 0.5)) - 0.5, 4),
                ],
                "resolution": [int(w), int(h)],
                "target_pos": 5,
                "threshold": DEFAULT_SCRIPT_DEFAULTS["match_threshold"],
                "rgb": False,
            })
    return saved


def _tap_index_from_frame(path: str) -> int:
    base = os.path.basename(path)
    digits = "".join(ch for ch in base if ch.isdigit())
    try:
        return int(digits)
    except ValueError:
        return -1


def _relative_path(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path


def _asset_dir_for(source: str, job_id: str | None) -> str:
    base = os.path.basename(os.path.normpath(source)) or "recording"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_"
                   for ch in base)
    suffix = job_id or time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(SCRIPT_ASSETS_DIR, f"{safe}_{suffix}")


def _video_parts(source: str) -> list[str]:
    if os.path.isfile(source):
        return [source]
    manifest = os.path.join(source, "session.json")
    if os.path.isdir(source) and os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [os.path.join(source, p) for p in data.get("parts", [])
                    if os.path.isfile(os.path.join(source, p))]
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _frame_at(cv2, metas, t: float):
    acc = 0.0
    for idx, (path, fps, dur) in enumerate(metas):
        if t <= acc + dur or idx == len(metas) - 1:
            local = max(0.0, t - acc)
            cap = cv2.VideoCapture(path)
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(local * fps))
            ok, frame = cap.read()
            cap.release()
            return frame if ok else None
        acc += dur
    return None


def build_generation_prompt(taps: list[dict], frames: list[str],
                            templates: list[dict],
                            meta: dict) -> str:
    frame_lines = "\n".join(
        f"- taps[{_tap_index_from_frame(p)}] 觸控前畫面：{p}"
        for p in frames) or "（無關鍵幀可用，請依 taps 的時間與座標推理）"
    template_lines = "\n".join(
        f"- taps[{t.get('tap_index')}] Airtest-like Template："
        f"image={t.get('image')} record_pos={t.get('record_pos')} "
        f"resolution={t.get('resolution')} target_pos={t.get('target_pos')} "
        f"threshold={t.get('threshold')} rgb={t.get('rgb')}"
        for t in templates) or "（無模板圖可用，才使用座標重放）"
    return f"""你是遊戲自動化腳本產生器。使用者錄了一段親手示範的遊戲操作，以下是錄影期間 getevent 實測到的**每一次觸控原始資料**（taps.json）與每次觸控前的畫面截圖。請你**完整計算並產出可重放的腳本 YAML**。

# 觸控原始資料（taps.json，時間單位秒，nx/ny 為 0~1 正規化座標）
```json
{json.dumps(taps, ensure_ascii=False, indent=1)}
```

# 每次觸控前的畫面截圖（用你的工具開圖檢視，理解每一步在點什麼）
{frame_lines}

# 已裁切的按鈕/點擊模板（用於 tap_image / tap_scene）
{template_lines}

# 腳本 schema（你要輸出的格式）
- 頂層欄位：`name`（腳本名）、`description`（這段流程在做什麼）、`defaults`（等待預設）、`steps`（動作序列）
- defaults 建議固定輸出：
  - `visual_timeout: 60`（tap_image/tap_scene 找模板最多等待秒數）
  - `until_timeout: 120`（until / wait_scene 最多等待秒數）
  - `stable_timeout: 45`（有 wait_after 的操作後，最多等待載入/轉場穩定秒數）
  - `match_interval: 1.0`（圖片比對輪詢間隔）
  - `match_threshold: 0.72`（圖片比對門檻，執行器會限制在 0.6~0.8）
- steps 支援的 action：
  - `tap`：欄位 x, y（**必須直接取自對應 tap 的 nx/ny，不得自行估計**）
  - `tap_image`：欄位 image/template 或 templates（使用上方模板路徑做圖片比對，找到後點擊模板 target_pos），可加 threshold/timeout/region/record_pos/resolution/target_pos/rgb
  - `tap_scene`：先用 anchor/scene 驗證目前畫面，再用 image/template/templates 點擊；沒有 image 時才退回 x/y
  - `long_press`：x, y, duration_ms
  - `swipe`：x1, y1, x2, y2（取自 nx/ny 與 end_nx/end_ny）, duration_ms
  - `wait`：seconds（純等待步驟）
  - `wait_scene`：等待 image/template 或 scene/anchor 出現
- 每步可帶：`name`（具體中文名稱，例「點擊 出擊按鈕」）、`wait_after`（該步後等待秒數）
- 每步可帶畫面驗證：`anchor` / `scene`（操作前必須出現的模板）、`until`（操作後必須等到的模板）
- 圖片比對欄位可用：`image` 或 `template`、`threshold`（建議 0.6~0.8，預設 0.72）、`timeout`、`region: [x1, y1, x2, y2]`
- Airtest-like 欄位：`record_pos`（相對畫面中心位置）、`resolution`（錄製解析度）、`target_pos`（1~9 九宮格點擊位置，5=中心）、`rgb`（預設 false，灰階比對）
- 若一個按鈕可能有多種外觀，可用 `templates: [{{image, record_pos, resolution, target_pos, threshold, rgb}}, ...]`

# 生成規則（比照 GameTestAi 的精神）
1. 依 taps 時間順序轉成 steps；kind 對應 action（tap/long_press/swipe）。
2. 若該 tap 有「已裁切模板」，穩定按鈕/圖示/可重複 UI 優先產生 `tap_image`，image/record_pos/resolution/target_pos/threshold/rgb 必須照抄上方 Template；只有模板不穩或畫面過場才使用 `tap`。
3. 重要步驟請加 `until` 驗證下一個畫面；容易誤點的步驟請加 `anchor` 或 `scene` 驗證目前畫面；涉及遊戲啟動、下載、Loading、戰鬥結算或轉場，timeout/ until_timeout 請用 90~180 秒，不要太短。
4. 兩次觸控的實際間隔反映成前一步的 `wait_after`（間隔近取 1~2 秒；有載入/轉場依畫面判斷加長，上限 90；不要把實測 30 秒以上的載入硬砍成 30）。
5. 看截圖判斷：若某次觸控落在過場/載入畫面（畫面模糊、無穩定按鈕），該點很可能是使用者在等待時的無意義點擊——改成 `wait` 或 `wait_scene` 步驟並在 name 註明，不要保留成 tap。
6. 每步命名要具體（看圖說出點的是什麼按鈕/區域），不要只寫「點擊」。
7. 若某步畫面涉及 登入/帳密/付費/購買/轉蛋/PVP 排位：保留該步但加 `risk: true` 與 `risk_reason`，name 前加「⚠ 」。
8. `description` 用一兩句話總結整段流程的目的。
9. 座標鐵則：所有 x/y/x1/y1/x2/y2 一律照抄 taps.json 的正規化值，禁止修改或發明座標。

# 輸出格式（最終回覆務必包含此區塊，區塊內只放 YAML）
AUTOGAMETEST_SCRIPT_YAML:
```yaml
name: ...
description: ...
defaults:
  visual_timeout: 60
  until_timeout: 120
  stable_timeout: 45
  match_interval: 1.0
  match_threshold: 0.72
steps:
  - action: tap_image
    name: ...
    image: data/scripts/assets/.../templates/tap00_template.png
    record_pos: [0.0, 0.31]
    resolution: [1280, 720]
    target_pos: 5
    rgb: false
    threshold: 0.72
    timeout: 60
    until: data/scripts/assets/.../templates/tap01_template.png
    until_timeout: 120
    wait_after: 2.0
```

補充資訊：來源錄影 `{meta.get('source', '')}`；預計在 `{meta.get('emulator', 'ldplayer')}` / `{meta.get('serial', 'emulator-5554')}` 重放{('；執行前會啟動 ' + meta['package']) if meta.get('package') else ''}。"""


def extract_script_yaml(text: str) -> dict | None:
    marker = "AUTOGAMETEST_SCRIPT_YAML"
    idx = (text or "").find(marker)
    if idx < 0 or yaml is None:
        return None
    tail = text[idx + len(marker):].lstrip(" :\n\r\t")
    if tail.startswith("```"):
        first_nl = tail.find("\n")
        if first_nl >= 0:
            tail = tail[first_nl + 1:]
        end = tail.find("```")
        if end >= 0:
            tail = tail[:end]
    try:
        data = yaml.safe_load(tail.strip())
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def _has_image_spec(data: dict) -> bool:
    if any(isinstance(data.get(k), str) and data.get(k).strip()
           for k in ("image", "template")):
        return True
    templates = data.get("templates")
    return isinstance(templates, list) and any(
        isinstance(item, str) and item.strip()
        or isinstance(item, dict) and _has_image_spec(item)
        for item in templates)


def _clamp_float(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _clean_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
    return default


def _clean_visual_value(value):
    if value in (None, "", []):
        return None
    if isinstance(value, str):
        return value.strip()[:500]
    if isinstance(value, list):
        items = [_clean_visual_value(v) for v in value]
        return [v for v in items if v is not None]
    if isinstance(value, dict):
        out = {}
        for key in ("image", "template"):
            if isinstance(value.get(key), str) and value.get(key).strip():
                out[key] = value[key].strip()[:500]
        if isinstance(value.get("templates"), list):
            templates = [_clean_visual_value(v) for v in value["templates"]]
            templates = [v for v in templates if v is not None]
            if templates:
                out["templates"] = templates
        if value.get("threshold") is not None:
            out["threshold"] = _clamp_float(
                value.get("threshold"), MIN_MATCH_THRESHOLD,
                MAX_MATCH_THRESHOLD, DEFAULT_SCRIPT_DEFAULTS["match_threshold"])
        for key in ("timeout", "interval", "scan_step", "max_points"):
            if isinstance(value.get(key), (int, float)):
                out[key] = value[key]
        if value.get("target_pos") is not None:
            try:
                out["target_pos"] = max(1, min(9, int(value["target_pos"])))
            except (TypeError, ValueError):
                pass
        if isinstance(value.get("record_pos"), (list, tuple)) and len(value["record_pos"]) == 2:
            try:
                out["record_pos"] = [float(value["record_pos"][0]), float(value["record_pos"][1])]
            except (TypeError, ValueError):
                pass
        if isinstance(value.get("resolution"), (list, tuple)) and len(value["resolution"]) == 2:
            try:
                out["resolution"] = [int(value["resolution"][0]), int(value["resolution"][1])]
            except (TypeError, ValueError):
                pass
        if value.get("rgb") is not None:
            out["rgb"] = _clean_bool(value.get("rgb"))
        if isinstance(value.get("region"), (list, tuple)) and len(value["region"]) == 4:
            try:
                out["region"] = [float(v) for v in value["region"]]
            except (TypeError, ValueError):
                pass
        for key in ("all", "any"):
            if isinstance(value.get(key), list):
                nested = [_clean_visual_value(v) for v in value[key]]
                nested = [v for v in nested if v is not None]
                if nested:
                    out[key] = nested
        return out or None
    return None


def _copy_visual_fields(src: dict, dst: dict) -> None:
    for key in ("image", "template"):
        if isinstance(src.get(key), str) and src.get(key).strip():
            dst[key] = src[key].strip()[:500]
    if isinstance(src.get("templates"), list):
        templates = [_clean_visual_value(v) for v in src["templates"]]
        templates = [v for v in templates if v is not None]
        if templates:
            dst["templates"] = templates
    if src.get("threshold") is not None:
        dst["threshold"] = _clamp_float(
            src.get("threshold"), MIN_MATCH_THRESHOLD,
            MAX_MATCH_THRESHOLD, DEFAULT_SCRIPT_DEFAULTS["match_threshold"])
    for key in ("timeout", "interval", "scan_step",
                "max_points", "anchor_timeout", "until_timeout"):
        if isinstance(src.get(key), (int, float)):
            dst[key] = src[key]
    if src.get("target_pos") is not None:
        try:
            dst["target_pos"] = max(1, min(9, int(src["target_pos"])))
        except (TypeError, ValueError):
            pass
    if isinstance(src.get("record_pos"), (list, tuple)) and len(src["record_pos"]) == 2:
        try:
            dst["record_pos"] = [float(src["record_pos"][0]), float(src["record_pos"][1])]
        except (TypeError, ValueError):
            pass
    if isinstance(src.get("resolution"), (list, tuple)) and len(src["resolution"]) == 2:
        try:
            dst["resolution"] = [int(src["resolution"][0]), int(src["resolution"][1])]
        except (TypeError, ValueError):
            pass
    if src.get("rgb") is not None:
        dst["rgb"] = _clean_bool(src.get("rgb"))
    if isinstance(src.get("region"), (list, tuple)) and len(src["region"]) == 4:
        try:
            dst["region"] = [float(v) for v in src["region"]]
        except (TypeError, ValueError):
            pass
    if isinstance(src.get("tap_offset"), (list, tuple)) and len(src["tap_offset"]) == 2:
        try:
            dst["tap_offset"] = [float(src["tap_offset"][0]), float(src["tap_offset"][1])]
        except (TypeError, ValueError):
            pass
    elif isinstance(src.get("tap_offset"), dict):
        try:
            dst["tap_offset"] = {
                "x": float(src["tap_offset"].get("x", src["tap_offset"].get("dx", 0))),
                "y": float(src["tap_offset"].get("y", src["tap_offset"].get("dy", 0))),
            }
        except (TypeError, ValueError):
            pass
    for key in ("anchor", "scene", "until"):
        clean = _clean_visual_value(src.get(key))
        if clean is not None:
            dst[key] = clean


def _clean_defaults(value) -> dict:
    defaults = dict(DEFAULT_SCRIPT_DEFAULTS)
    if not isinstance(value, dict):
        return defaults
    for key, fallback in DEFAULT_SCRIPT_DEFAULTS.items():
        raw = value.get(key)
        if key == "match_threshold":
            defaults[key] = _clamp_float(
                raw, MIN_MATCH_THRESHOLD, MAX_MATCH_THRESHOLD, fallback)
        elif isinstance(raw, (int, float)):
            defaults[key] = max(0, raw)
        else:
            try:
                defaults[key] = max(0, float(raw))
            except (TypeError, ValueError):
                defaults[key] = fallback
    return defaults


def sanitize_generated(data: dict, taps: list[dict], meta: dict) -> tuple[dict, str]:
    """Safety pass over Codex's output. Returns (clean_script, error).

    Coordinates must match a recorded touch (tolerance) — the AI is not allowed
    to invent tap positions, only to drop/annotate/convert them to waits.
    """
    allowed_coords = set()
    for tp in taps:
        for pair in ((tp.get("nx"), tp.get("ny")),
                     (tp.get("end_nx"), tp.get("end_ny"))):
            if pair[0] is not None:
                allowed_coords.add((round(float(pair[0]), 2),
                                    round(float(pair[1]), 2)))

    def coord_ok(x, y) -> bool:
        return (round(float(x), 2), round(float(y), 2)) in allowed_coords

    steps_in = data.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        return {}, "Codex 輸出沒有 steps"
    clean_steps = []
    for i, s in enumerate(steps_in):
        if not isinstance(s, dict):
            return {}, f"step {i+1} 不是物件"
        action = s.get("action")
        out = {"action": action,
               "name": str(s.get("name", "") or f"step {i+1}")[:120]}
        wa = s.get("wait_after")
        if isinstance(wa, (int, float)) and 0 <= float(wa) <= 90:
            out["wait_after"] = round(float(wa), 1)
        if s.get("risk"):
            out["risk"] = True
            reason = str(s.get("risk_reason", "") or "").strip()
            if reason:
                out["risk_reason"] = reason[:200]
        if action == "wait":
            secs = s.get("seconds", 2)
            out["seconds"] = round(min(max(float(secs), 0.2), 60), 1) \
                if isinstance(secs, (int, float)) else 2.0
        elif action in ("tap_image", "tap_scene", "wait_scene"):
            _copy_visual_fields(s, out)
            if action == "tap_image" and not _has_image_spec(out):
                return {}, f"step {i+1} tap_image 缺少 image/template"
            if action == "tap_scene":
                has_image = _has_image_spec(out)
                has_coord = isinstance(s.get("x"), (int, float)) and isinstance(s.get("y"), (int, float))
                if has_coord:
                    if not coord_ok(s["x"], s["y"]):
                        return {}, f"step {i+1} tap_scene 座標不在錄影實測觸控中"
                    out["x"], out["y"] = round(float(s["x"]), 4), round(float(s["y"]), 4)
                if not has_image and not has_coord:
                    return {}, f"step {i+1} tap_scene 缺少 image/template 或錄影座標"
                if not (out.get("anchor") or out.get("scene")):
                    return {}, f"step {i+1} tap_scene 缺少 anchor 或 scene"
            if action == "wait_scene" and not (
                    _has_image_spec(out) or out.get("anchor") or out.get("scene")):
                return {}, f"step {i+1} wait_scene 缺少 image/template/anchor/scene"
        elif action in ("tap", "long_press"):
            x, y = s.get("x"), s.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return {}, f"step {i+1} 缺少座標"
            if not coord_ok(x, y):
                return {}, f"step {i+1} 座標 ({x},{y}) 不在錄影實測觸控中（AI 不得發明座標）"
            out["x"], out["y"] = round(float(x), 4), round(float(y), 4)
            if action == "long_press":
                ms = s.get("duration_ms", 600)
                out["duration_ms"] = int(min(max(int(ms), 400), 10000)) \
                    if isinstance(ms, (int, float)) else 600
        elif action == "swipe":
            coords = [s.get(k) for k in ("x1", "y1", "x2", "y2")]
            if any(not isinstance(v, (int, float)) for v in coords):
                return {}, f"step {i+1} swipe 缺少座標"
            if not coord_ok(coords[0], coords[1]):
                return {}, f"step {i+1} swipe 起點不在錄影實測觸控中"
            out.update({k: round(float(v), 4)
                        for k, v in zip(("x1", "y1", "x2", "y2"), coords)})
            ms = s.get("duration_ms", 300)
            out["duration_ms"] = int(min(max(int(ms), 100), 10000)) \
                if isinstance(ms, (int, float)) else 300
        else:
            return {}, f"step {i+1} 動作不支援: {action}"
        clean_steps.append(out)

    script = {
        "name": str(data.get("name", "") or "").strip()[:80] or "未命名腳本",
        "source": meta.get("source", ""),
        "emulator": meta.get("emulator") or "ldplayer",
        "serial": meta.get("serial") or "emulator-5554",
        "package": meta.get("package", ""),
        "description": str(data.get("description", "") or "").strip()[:500],
        "generated_by": "codex",
        "defaults": _clean_defaults(data.get("defaults")),
        "steps": clean_steps,
    }
    if meta.get("package"):
        script["steps"] = ([{"action": "launch_app",
                             "name": f"啟動 {meta['package']}",
                             "wait_after": 8.0}] + script["steps"])
    err = scripts.validate_script(script)
    return (script, "") if not err else ({}, err)


def generate(source: str, name: str = "", package: str = "",
             serial: str = "", emulator: str = "",
             job_id: str | None = None, timeout: int = 1800,
             model: str | None = None,
             reasoning_effort: str | None = None) -> dict:
    if job_id:
        store.update_job(job_id, status="running")

    def progress(text: str) -> None:
        print(text, flush=True)
        if job_id:
            store.update_job(job_id, progress=text)

    if not os.path.exists(source):
        return _finish(job_id, {"ok": False, "error": f"找不到錄影：{source}"})
    taps = scripts.load_taps(source)
    if not taps:
        return _finish(job_id, {"ok": False,
                                "error": "此錄影沒有 taps.json（錄影時需在模擬器"
                                         "視窗或操控分頁點擊）"})
    meta = {"source": source, "package": package,
            "serial": serial or "emulator-5554",
            "emulator": emulator or "ldplayer"}

    frames: list[str] = []
    templates: list[str] = []
    if job_id:
        progress("從影片抽取每步觸控前的關鍵幀…")
        asset_dir = _asset_dir_for(source, job_id)
        frames = extract_keyframes(
            source, taps, os.path.join(asset_dir, "frames"))
        templates = extract_touch_templates(
            frames, taps, os.path.join(asset_dir, "templates"))
    progress(f"共 {len(taps)} 次觸控、{len(frames)} 張關鍵幀、"
             f"{len(templates)} 張模板，"
             "交給 Codex 完整生成腳本…")

    generated = None
    detail = ""
    prompt = build_generation_prompt(taps, frames, templates, meta)
    try:
        result = ai_runner.run_with_fallback(
            prompt, cwd=ROOT, timeout=timeout, engine="codex",
            codex_sandbox="workspace-write",
            codex_model=model, codex_reasoning_effort=reasoning_effort)
        if result.get("ok"):
            raw = extract_script_yaml(result.get("output", ""))
            if raw:
                generated, err = sanitize_generated(raw, taps, meta)
                if err:
                    detail = f"Codex 輸出未過安全驗證：{err}"
                    generated = None
            else:
                detail = "Codex 回覆中沒有 AUTOGAMETEST_SCRIPT_YAML 區塊"
        else:
            detail = result.get("reason", "Codex 執行失敗")
    except Exception as e:   # AI failure must not lose the recording
        detail = f"Codex 生成失敗：{e}"

    if generated:
        if name:
            generated["name"] = name   # 使用者取的名字優先
        saved = scripts.save_script(generated)
        annotated = True
    else:
        # fallback：確定性骨架草稿，錄影不白費
        skeleton = scripts.build_skeleton(
            source, name=name, package=package,
            serial=meta["serial"], emulator=meta["emulator"])
        saved = scripts.save_script(skeleton)
        annotated = False

    return _finish(job_id, {
        "ok": True,
        "script_id": saved["id"],
        "script_name": saved.get("name", ""),
        "n_steps": len(saved.get("steps", [])),
        "annotated": annotated,
        "frames": len(frames),
        "templates": len(templates),
        "detail": "" if annotated else f"以草稿骨架儲存（{detail or 'Codex 未產出'}）",
    })


def _finish(job_id: str | None, result: dict) -> dict:
    if job_id:
        if result.get("ok"):
            note = f"，{result['detail']}" if result.get("detail") else ""
            txt = (f"[engine=codex] 腳本已生成：「{result.get('script_name','')}」"
                   f"（{result.get('n_steps')} 步，Codex 生成 "
                   f"{'成功' if result.get('annotated') else '失敗→草稿骨架'}{note}）"
                   f" script_id={result.get('script_id')}")
        else:
            txt = f"[engine=codex] 生成失敗：{result.get('error','')}"
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            engine_used="codex" if result.get("ok") else None,
            script_id=result.get("script_id"),
            result=txt)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate a script from a recording (Codex)")
    ap.add_argument("--job", help="job id（payload 內含 source/name 等）")
    ap.add_argument("--source", default="", help="錄影路徑（mp4 或多段資料夾）")
    ap.add_argument("--name", default="", help="腳本名稱")
    ap.add_argument("--package", default="", help="執行前要啟動的 app package")
    ap.add_argument("--serial", default="", help="預設目標裝置")
    ap.add_argument("--emulator", default="", help="模擬器類型")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--model", default=None)
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--engine", default="codex", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    source, name = args.source, args.name
    package, serial, emulator = args.package, args.serial, args.emulator
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr)
            return 2
        p = job.get("payload", {})
        source = source or p.get("source", "")
        name = name or p.get("name", "")
        package = package or p.get("package", "")
        serial = serial or p.get("serial", "")
        emulator = emulator or p.get("emulator", "")

    if not source:
        print("缺少 --source", file=sys.stderr)
        return 2

    result = generate(source, name=name, package=package, serial=serial,
                      emulator=emulator, job_id=args.job,
                      timeout=args.timeout, model=args.model,
                      reasoning_effort=args.reasoning_effort)
    if result.get("ok"):
        print(f"完成：{result.get('script_id')}（{result.get('n_steps')} 步，"
              f"Codex 生成 {'成功' if result.get('annotated') else '失敗→草稿'}）")
        return 0
    print(f"失敗：{result.get('error','')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
