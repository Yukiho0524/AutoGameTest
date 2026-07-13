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
import re
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

# Pick a frame before the recorded tap. Screenrecord/getevent clocks can drift,
# so try nearest-before candidates first and fall back to older stable frames.
FRAME_PRE_TAP_OFFSETS = (
    0.03, 0.05, 0.08, 0.10, 0.18, 0.30, 0.45,
    0.70, 1.00, 1.50, 2.00, 3.00,
)
FRAME_STABILITY_GAP = 0.12
FRAME_STABLE_DELTA = 8.0
FRAME_SCENE_CHANGE_DELTA = 12.0
FRAME_SCENE_CORRECTION_MAX_OFFSET = 1.50
FRAME_TAP_REGION_CHANGE_DELTA = 35.0
FRAME_TAP_REGION_CORRECTION_MAX_OFFSET = 1.00
CONTEXT_CROP_HALF_W_RATIO = 0.16
CONTEXT_CROP_HALF_H_RATIO = 0.13


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
        prev_time = _tap_time(taps[i - 1]) if i > 0 else None
        frame = _pre_tap_frame(cv2, metas, tp, prev_time=prev_time)
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
    context_dir = os.path.join(os.path.dirname(out_dir), "contexts")
    os.makedirs(context_dir, exist_ok=True)
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
        half_w = max(32, min(110, int(w * 0.06)))
        half_h = max(24, min(72, int(h * 0.055)))
        x1, y1, x2, y2 = _crop_bounds(w, h, cx, cy, half_w, half_h)
        if x2 - x1 < 12 or y2 - y1 < 12:
            continue
        crop = frame[y1:y2, x1:x2]
        if not _template_is_distinctive(cv2, crop):
            continue
        context_image = _write_context_crop(
            cv2, frame, idx, cx, cy, context_dir)
        path = os.path.join(out_dir, f"tap{idx:02d}_template.png")
        if cv2.imwrite(path, crop):
            saved.append({
                "tap_index": idx,
                "image": _relative_path(path),
                "context_image": context_image,
                "template_size": [int(x2 - x1), int(y2 - y1)],
                "record_pos": [
                    round(float(tap.get("nx", 0.5)) - 0.5, 4),
                    round(float(tap.get("ny", 0.5)) - 0.5, 4),
                ],
                "resolution": [int(w), int(h)],
                "target_pos": 5,
                "threshold": DEFAULT_SCRIPT_DEFAULTS["match_threshold"],
                "rgb": False,
                "allow_full_search": False,
            })
    return saved


def _crop_bounds(width: int, height: int, cx: int, cy: int,
                 half_w: int, half_h: int) -> tuple[int, int, int, int]:
    x1, x2 = max(0, cx - half_w), min(width, cx + half_w)
    y1, y2 = max(0, cy - half_h), min(height, cy + half_h)
    return x1, y1, x2, y2


def _write_context_crop(cv2, frame, idx: int, cx: int, cy: int,
                        context_dir: str) -> str:
    h, w = frame.shape[:2]
    half_w = max(96, min(260, int(w * CONTEXT_CROP_HALF_W_RATIO)))
    half_h = max(64, min(170, int(h * CONTEXT_CROP_HALF_H_RATIO)))
    x1, y1, x2, y2 = _crop_bounds(w, h, cx, cy, half_w, half_h)
    if x2 - x1 < 12 or y2 - y1 < 12:
        return ""
    path = os.path.join(context_dir, f"tap{idx:02d}_context.png")
    if cv2.imwrite(path, frame[y1:y2, x1:x2]):
        return _relative_path(path)
    return ""


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


def _pre_tap_frame(cv2, metas, tap: dict, prev_time: float | None = None):
    tap_time = _tap_time(tap)
    samples = []
    for offset in _pre_tap_offsets(tap_time, prev_time):
        t = max(0.0, tap_time - offset)
        frame = _frame_at(cv2, metas, t)
        if frame is None:
            continue
        samples.append((offset, t, frame))
    if not samples:
        return None

    # If getevent is later than screenrecord, the nearest frames are already
    # after the tap. Find the latest scene change and use the older side.
    for (_, _, newer), (older_offset, _, older) in zip(samples, samples[1:]):
        if older_offset > FRAME_SCENE_CORRECTION_MAX_OFFSET + 1e-6:
            continue
        if _frame_delta(cv2, newer, older) >= FRAME_SCENE_CHANGE_DELTA:
            return older

    # Android "show taps" / pressed-state effects are often local only, so the
    # full frame still looks stable. If the tap area changes sharply, use the
    # older side before the touch feedback appears.
    for (_, _, newer), (older_offset, _, older) in zip(samples, samples[1:]):
        if older_offset > FRAME_TAP_REGION_CORRECTION_MAX_OFFSET + 1e-6:
            continue
        if _tap_region_delta(cv2, newer, older, tap) >= FRAME_TAP_REGION_CHANGE_DELTA:
            return older

    for _, t, frame in samples:
        prev = _frame_at(cv2, metas, max(0.0, t - FRAME_STABILITY_GAP))
        if prev is None or _frame_delta(cv2, prev, frame) <= FRAME_STABLE_DELTA:
            return frame
    return samples[0][2]


def _tap_time(tap: dict) -> float:
    try:
        return float(tap.get("t", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _pre_tap_offsets(tap_time: float,
                     prev_time: float | None = None) -> list[float]:
    offsets = list(FRAME_PRE_TAP_OFFSETS)
    if prev_time is None:
        return offsets
    gap = max(0.0, tap_time - prev_time)
    if gap <= 0:
        return [0.03]
    max_offset = gap - 0.03
    if max_offset <= 0:
        return [max(0.01, gap * 0.5)]
    bounded = [off for off in offsets if off <= max_offset]
    if bounded:
        return bounded
    return [max(0.01, min(0.03, gap * 0.5))]


def _frame_delta(cv2, a, b) -> float:
    try:
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        ga = cv2.resize(ga, (96, 54), interpolation=cv2.INTER_AREA)
        gb = cv2.resize(gb, (96, 54), interpolation=cv2.INTER_AREA)
        return float(cv2.absdiff(ga, gb).mean())
    except Exception:
        return 0.0


def _tap_region_delta(cv2, a, b, tap: dict) -> float:
    try:
        x, y = _tap_xy_for_frame(tap, a)
        h, w = a.shape[:2]
        rx = max(48, int(w * 0.055))
        ry = max(36, int(h * 0.055))
        x1, x2 = max(0, x - rx), min(w, x + rx)
        y1, y2 = max(0, y - ry), min(h, y + ry)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        ga = cv2.cvtColor(a[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(b[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        ga = cv2.resize(ga, (96, 54), interpolation=cv2.INTER_AREA)
        gb = cv2.resize(gb, (96, 54), interpolation=cv2.INTER_AREA)
        return float(cv2.absdiff(ga, gb).mean())
    except Exception:
        return 0.0


def _tap_xy_for_frame(tap: dict, frame) -> tuple[int, int]:
    h, w = frame.shape[:2]
    try:
        nx = float(tap.get("nx"))
        ny = float(tap.get("ny"))
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            return int(round(nx * w)), int(round(ny * h))
    except (TypeError, ValueError):
        pass
    try:
        return int(tap.get("x")), int(tap.get("y"))
    except (TypeError, ValueError):
        return w // 2, h // 2


def _template_is_distinctive(cv2, crop) -> bool:
    try:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        center = gray[h // 4: max(h // 4 + 1, 3 * h // 4),
                      w // 4: max(w // 4 + 1, 3 * w // 4)]
        _, full_std = cv2.meanStdDev(gray)
        _, center_std = cv2.meanStdDev(center)
        edges = cv2.Canny(center, 50, 150)
        edge_density = (
            cv2.countNonZero(edges) / max(1, edges.shape[0] * edges.shape[1]))
        return not (
            float(center_std[0][0]) < 6.0
            and edge_density < 0.015
            and float(full_std[0][0]) < 35.0
        )
    except Exception:
        return True


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
        f"threshold={t.get('threshold')} rgb={t.get('rgb')} "
        f"allow_full_search={t.get('allow_full_search')} "
        f"template_size={t.get('template_size')} "
        f"context_image={t.get('context_image') or '無'}"
        for t in templates) or "（無模板圖可用，才使用座標重放）"
    return f"""你是遊戲自動化腳本產生器。使用者錄了一段親手示範的遊戲操作，以下是錄影期間 getevent 實測到的**每一次觸控原始資料**（taps.json）與每次觸控前的畫面截圖。請你**完整計算並產出可重放的腳本 YAML**。

# 觸控原始資料（taps.json，時間單位秒，nx/ny 為 0~1 正規化座標）
```json
{json.dumps(taps, ensure_ascii=False, indent=1)}
```

# 每次觸控前的畫面截圖（已盡量取點擊前一幀/穩定前置幀；用你的工具開圖檢視，理解每一步在點什麼）
{frame_lines}

# 已裁切的按鈕/點擊模板（用於 tap_image / tap_scene）
{template_lines}

說明：`image` 是執行時用來比對的精準小模板；`context_image` 是較大的語意預覽圖，只用來看清完整按鈕/圖示與周圍 UI，不要把 `context_image` 填進 YAML 的 image/template。

# 腳本 schema（你要輸出的格式）
- 頂層欄位：`name`（腳本名）、`description`（這段流程在做什麼）、`defaults`（等待預設）、`steps`（動作序列）；系統會自動保存 `game_id/game_name/package/emulator/serial`
- defaults 建議固定輸出：
  - `visual_timeout: 60`（tap_image/tap_scene 找模板最多等待秒數）
  - `until_timeout: 120`（until / wait_scene 最多等待秒數）
  - `stable_timeout: 45`（有 wait_after 的操作後，最多等待載入/轉場穩定秒數）
  - `match_interval: 1.0`（圖片比對輪詢間隔）
  - `match_threshold: 0.72`（圖片比對門檻，執行器會限制在 0.6~0.8）
- steps 支援的 action：
  - `tap`：欄位 x, y（**必須直接取自對應 tap 的 nx/ny，不得自行估計**）
  - `tap_image`：欄位 image/template 或 templates（使用上方模板路徑做圖片比對，找到後點擊模板 target_pos），可加 threshold/timeout/region/record_pos/resolution/target_pos/rgb/allow_full_search
  - `tap_scene`：先用 anchor/scene 驗證目前畫面，再用 image/template/templates 點擊；沒有 image 時才退回 x/y
  - `long_press`：x, y, duration_ms
  - `swipe`：x1, y1, x2, y2（取自 nx/ny 與 end_nx/end_ny）, duration_ms
  - `wait`：seconds（純等待步驟）
  - `wait_scene`：等待 image/template 或 scene/anchor 出現
- 每步可帶：`name`（具體中文名稱，例「點擊 出擊按鈕」）、`wait_after`（該步後等待秒數）、`on_timeout: skip`（圖片 timeout 找不到時略過本步並繼續下一步；預設 fail）
- 每步可帶畫面驗證：`anchor` / `scene`（操作前必須出現的模板）、`until`（操作後必須等到的模板）
- 圖片比對欄位可用：`image` 或 `template`、`threshold`（建議 0.6~0.8，預設 0.72）、`timeout`、`region: [x1, y1, x2, y2]`
- Airtest-like 欄位：`record_pos`（相對畫面中心位置）、`resolution`（錄製解析度）、`target_pos`（1~9 九宮格點擊位置，5=中心）、`rgb`（預設 false，灰階比對）、`allow_full_search`（預設 false，有錄製位置就只在附近找）
- 若一個按鈕可能有多種外觀，可用 `templates: [{{image, record_pos, resolution, target_pos, threshold, rgb, allow_full_search}}, ...]`

# 生成規則（比照 GameTestAi 的精神）
1. 依 taps 時間順序轉成 steps；kind 對應 action（tap/long_press/swipe）。
2. 若該 tap 有「已裁切模板」，穩定按鈕/圖示/可重複 UI 優先產生 `tap_image`，image/record_pos/resolution/target_pos/threshold/rgb/allow_full_search 必須照抄上方 Template；只有模板不穩、看起來像背景/空白/廣告雜訊，或畫面過場時才使用 `tap`。
3. 重要步驟請加 `until` 驗證下一個畫面；`until` 必須是下一畫面的穩定錨點，不要使用背景空白、動畫殘影或只在錄影中偶然出現的小雜訊。容易誤點的步驟請加 `anchor` 或 `scene` 驗證目前畫面；涉及遊戲啟動、下載、Loading、戰鬥結算或轉場，timeout/ until_timeout 請用 90~180 秒，不要太短。
4. 兩次觸控的實際間隔反映成前一步的 `wait_after`。若間隔小於 1 秒，請保留短間隔（約 0.05~0.9 秒）以支援連點、雙擊、快速確認，不要自動放大成 1~2 秒；只有有載入/轉場時才依畫面判斷加長，上限 90；不要把實測 30 秒以上的載入硬砍成 30。
5. 看截圖判斷：若某次觸控落在過場/載入畫面（畫面模糊、無穩定按鈕），該點很可能是使用者在等待時的無意義點擊——改成 `wait` 或 `wait_scene` 步驟並在 name 註明，不要保留成 tap。
6. 每步命名要具體（看圖說出點的是什麼按鈕/區域），不要只寫「點擊」。
7. 若某步畫面涉及 登入/帳密/付費/購買/轉蛋/PVP 排位：保留該步但加 `risk: true` 與 `risk_reason`，name 前加「⚠ 」。
8. `description` 用一兩句話總結整段流程的目的。
9. 座標鐵則：所有 x/y/x1/y1/x2/y2 一律照抄 taps.json 的正規化值，禁止修改或發明座標。
10. 關鍵幀與模板代表「按下前」的畫面；若截圖看起來已經是按下後結果，優先把該 tap 視為時間軸偏移/無效點，不要用它當前一步的 `until` 或穩定模板。
11. 若某一步是可選畫面、彈窗、活動入口、廣告、教學提示、或已可能被前一步處理掉，請加 `on_timeout: skip`；執行時該圖片 timeout 找不到會跳下一步繼續找下一張。關鍵流程、付費/抽取確認、不可跳過的主路徑不要加。
12. 若小模板看不出完整樣貌，請打開同一列的 `context_image` 或該 tap 的觸控前畫面來理解語意；輸出的 YAML 仍使用 `image` 小模板路徑。
13. 若連續 taps 的時間差很短且畫面沒有明顯轉場，視為快速連點/雙擊/連續確認；不要插入 `wait_scene` 或長 `until`，也不要把後一個 tap 的關鍵幀解讀成上一個 tap 前的畫面。

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
    allow_full_search: false
    threshold: 0.72
    timeout: 60
    on_timeout: skip
    until: data/scripts/assets/.../templates/tap01_template.png
    until_timeout: 120
    wait_after: 2.0
```

補充資訊：來源錄影 `{meta.get('source', '')}`；遊戲 `{meta.get('game_name') or meta.get('game_id') or '未指定'}`；預計在 `{meta.get('emulator', 'ldplayer')}` / `{meta.get('serial', 'emulator-5554')}` 重放{('；執行前會啟動 ' + meta['package']) if meta.get('package') else ''}。"""


def extract_script_yaml(text: str) -> dict | None:
    marker = "AUTOGAMETEST_SCRIPT_YAML"
    if yaml is None:
        return None
    raw_text = text or ""
    candidates: list[tuple[str, bool]] = []

    for match in re.finditer(re.escape(marker), raw_text, flags=re.IGNORECASE):
        tail = raw_text[match.end():]
        candidates.append((_strip_yaml_preamble(tail), True))

    for block in _fenced_yaml_blocks(raw_text):
        if marker.lower() in block.lower():
            _, block = re.split(re.escape(marker), block, maxsplit=1,
                                flags=re.IGNORECASE)
            candidates.append((_strip_yaml_preamble(block), True))
        else:
            candidates.append((block, False))

    raw_candidate = _raw_yaml_candidate(raw_text)
    if raw_candidate:
        candidates.append((raw_candidate, False))

    for candidate, marker_backed in candidates:
        data = _parse_script_yaml_candidate(candidate)
        if isinstance(data, dict) and (
                marker_backed or _looks_like_script_yaml(data)):
            return data
    return None


def _strip_yaml_preamble(text: str) -> str:
    tail = str(text or "").lstrip(" \t\r\n:：=-`")
    if tail.startswith("```"):
        first_nl = tail.find("\n")
        if first_nl >= 0:
            tail = tail[first_nl + 1:]
        end = tail.find("```")
        if end >= 0:
            tail = tail[:end]
    return tail.strip()


def _fenced_yaml_blocks(text: str) -> list[str]:
    blocks = []
    pattern = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)```",
                         flags=re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(text or ""):
        block = match.group(1).strip()
        if block:
            blocks.append(block)
    return blocks


def _raw_yaml_candidate(text: str) -> str:
    lines = str(text or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*(name|description|defaults|steps)\s*:", line):
            start = i
            break
    if start is None:
        return ""
    return "\n".join(lines[start:]).strip()


def _parse_script_yaml_candidate(text: str) -> dict | None:
    candidate = str(text or "").strip()
    if not candidate:
        return None
    try:
        data = yaml.safe_load(candidate)
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        pass
    trimmed = _trim_yaml_to_script(candidate)
    if trimmed and trimmed != candidate:
        try:
            data = yaml.safe_load(trimmed)
            return data if isinstance(data, dict) else None
        except yaml.YAMLError:
            return None
    return None


def _trim_yaml_to_script(text: str) -> str:
    lines = str(text or "").splitlines()
    if not lines:
        return ""
    best = ""
    for end in range(len(lines), 0, -1):
        chunk = "\n".join(lines[:end]).strip()
        if not chunk:
            continue
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and _looks_like_script_yaml(data):
            best = chunk
            break
    return best


def _looks_like_script_yaml(data: dict) -> bool:
    steps = data.get("steps")
    return isinstance(steps, list) and any(isinstance(step, dict)
                                           for step in steps)


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


def _clean_on_timeout(value) -> str:
    text = str(value or "").strip().lower()
    if text in ("skip", "continue", "next"):
        return "skip"
    if text == "fail":
        return "fail"
    return ""


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
        on_timeout = _clean_on_timeout(s.get("on_timeout"))
        if on_timeout:
            out["on_timeout"] = on_timeout
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
        "game_id": meta.get("game_id", ""),
        "game_name": meta.get("game_name", ""),
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


def build_visual_fallback(source: str, taps: list[dict], templates: list[dict],
                          name: str, meta: dict) -> dict:
    """Build a draft script that still uses recorded image templates."""
    script = scripts.build_skeleton(
        source,
        name=name,
        package=meta.get("package", ""),
        serial=meta.get("serial", ""),
        emulator=meta.get("emulator", ""),
        game_id=meta.get("game_id", ""),
        game_name=meta.get("game_name", ""),
    )
    template_by_tap: dict[int, dict] = {}
    for item in templates or []:
        if not isinstance(item, dict) or not item.get("image"):
            continue
        try:
            template_by_tap[int(item.get("tap_index"))] = item
        except (TypeError, ValueError):
            continue
    if not template_by_tap:
        return script

    visual_count = 0
    tap_index = 0
    new_steps = []
    for step in script.get("steps", []):
        if step.get("action") == "launch_app":
            new_steps.append(step)
            continue
        if tap_index >= len(taps):
            new_steps.append(step)
            continue
        current_tap = taps[tap_index]
        template = template_by_tap.get(tap_index)
        tap_index += 1
        if step.get("action") != "tap" or not template:
            new_steps.append(step)
            continue
        next_step = {
            "action": "tap_image",
            "name": step.get("name") or f"tap {tap_index}",
            "wait_after": step.get("wait_after", 0.2),
            "image": template.get("image"),
            "threshold": template.get(
                "threshold", DEFAULT_SCRIPT_DEFAULTS["match_threshold"]),
            "timeout": DEFAULT_SCRIPT_DEFAULTS["visual_timeout"],
            "target_pos": template.get("target_pos", 5),
            "record_pos": template.get("record_pos"),
            "resolution": template.get("resolution"),
            "rgb": bool(template.get("rgb", False)),
            "allow_full_search": bool(template.get("allow_full_search", False)),
        }
        if current_tap.get("kind") not in ("", "tap", None):
            next_step["name"] = f"{next_step['name']} ({current_tap.get('kind')})"
        new_steps.append({k: v for k, v in next_step.items()
                          if v not in (None, "", [])})
        visual_count += 1

    script["steps"] = new_steps
    script["description"] = (
        f"AI 註解失敗，已用錄影裁切模板建立圖片草稿；"
        f"{visual_count} 個 tap 使用 tap_image，其餘保留座標/手勢。")
    err = scripts.validate_script(script)
    if err:
        return scripts.build_skeleton(
            source,
            name=name,
            package=meta.get("package", ""),
            serial=meta.get("serial", ""),
            emulator=meta.get("emulator", ""),
            game_id=meta.get("game_id", ""),
            game_name=meta.get("game_name", ""),
        )
    return script


def generate(source: str, name: str = "", package: str = "",
             serial: str = "", emulator: str = "",
             game_id: str = "", game_name: str = "",
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
            "emulator": emulator or "ldplayer",
            "game_id": game_id,
            "game_name": game_name}

    frames: list[str] = []
    templates: list[dict] = []
    if job_id:
        progress("從影片抽取每步觸控前的關鍵幀…")
        asset_dir = _asset_dir_for(source, job_id)
        frames = extract_keyframes(
            source, taps, os.path.join(asset_dir, "frames"))
        templates = extract_touch_templates(
            frames, taps, os.path.join(asset_dir, "templates"))
    context_count = sum(1 for t in templates if t.get("context_image"))
    progress(f"共 {len(taps)} 次觸控、{len(frames)} 張關鍵幀、"
             f"{len(templates)} 張模板、{context_count} 張語意圖，"
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
                detail = "Codex 回覆中沒有可解析的 AUTOGAMETEST_SCRIPT_YAML/YAML 腳本"
        else:
            detail = result.get("reason", "Codex 執行失敗")
    except Exception as e:   # AI failure must not lose the recording
        detail = f"Codex 生成失敗：{e}"

    fallback_kind = ""
    if generated:
        if name:
            generated["name"] = name   # 使用者取的名字優先
        saved = scripts.save_script(generated)
        annotated = True
    else:
        # fallback：優先使用已裁出的圖片模板；沒有模板才退回座標骨架。
        skeleton = build_visual_fallback(source, taps, templates, name, meta)
        fallback_kind = "圖片草稿" if any(
            isinstance(step, dict)
            and step.get("action") in ("tap_image", "tap_scene", "wait_scene")
            for step in skeleton.get("steps", [])) else "座標草稿"
        saved = scripts.save_script(skeleton)
        annotated = False

    return _finish(job_id, {
        "ok": True,
        "script_id": saved["id"],
        "script_name": saved.get("name", ""),
        "n_steps": len(saved.get("steps", [])),
        "annotated": annotated,
        "fallback_kind": fallback_kind,
        "frames": len(frames),
        "templates": len(templates),
        "detail": "" if annotated else f"以{fallback_kind or '草稿'}儲存（{detail or 'Codex 未產出'}）",
    })


def _finish(job_id: str | None, result: dict) -> dict:
    if job_id:
        if result.get("ok"):
            note = f"，{result['detail']}" if result.get("detail") else ""
            generated_text = "成功" if result.get("annotated") else (
                f"失敗→{result.get('fallback_kind') or '草稿骨架'}")
            txt = (f"[engine=codex] 腳本已生成：「{result.get('script_name','')}」"
                   f"（{result.get('n_steps')} 步，Codex 生成 "
                   f"{generated_text}{note}）"
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
    ap.add_argument("--game-id", default="", help="綁定遊戲 id")
    ap.add_argument("--game-name", default="", help="綁定遊戲名稱")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--model", default=None)
    ap.add_argument("--reasoning-effort", default=None)
    ap.add_argument("--engine", default="codex", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    source, name = args.source, args.name
    package, serial, emulator = args.package, args.serial, args.emulator
    game_id, game_name = args.game_id, args.game_name
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
        game_id = game_id or p.get("game_id", "")
        game_name = game_name or p.get("game_name", "")

    if not source:
        print("缺少 --source", file=sys.stderr)
        return 2

    result = generate(source, name=name, package=package, serial=serial,
                      emulator=emulator,
                      game_id=game_id, game_name=game_name,
                      job_id=args.job,
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
