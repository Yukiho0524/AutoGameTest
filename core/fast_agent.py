"""Fast local decision layer for emulator agents.

The fast layer is intentionally conservative. It only executes actions when the
current screenshot matches a learned rule by exact SHA-256 or by a configured
average-hash distance. Unknown screens are handed back to Codex.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
import zlib
from datetime import datetime
from typing import Any

from . import adb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST_RULES_DIR = os.path.join(ROOT, "data", "fast_rules")
ARTIFACTS_DIR = os.path.join(ROOT, "data", "artifacts")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ALLOWED_ACTIONS = {"tap", "swipe", "wait", "launch_app", "stop_app", "screenshot", "report"}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or f"rule-{uuid.uuid4().hex[:8]}"


def _rules_path(game_id: str) -> str:
    return os.path.join(FAST_RULES_DIR, f"{_slugify(game_id)}.json")


def load_rules(game_id: str) -> dict:
    path = _rules_path(game_id)
    if not os.path.isfile(path):
        return {"version": 1, "game_id": game_id, "rules": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "game_id": game_id, "rules": []}
    if not isinstance(data, dict):
        return {"version": 1, "game_id": game_id, "rules": []}
    data.setdefault("version", 1)
    data.setdefault("game_id", game_id)
    data.setdefault("rules", [])
    if not isinstance(data["rules"], list):
        data["rules"] = []
    return data


def save_rules(game_id: str, data: dict) -> None:
    os.makedirs(FAST_RULES_DIR, exist_ok=True)
    data["version"] = 1
    data["game_id"] = game_id
    tmp = _rules_path(game_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _rules_path(game_id))


def _iter_png_chunks(data: bytes):
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        chunk_type = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        yield chunk_type, chunk
        if chunk_type == b"IEND":
            break


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_png_rgb(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()
    for chunk_type, chunk in _iter_png_chunks(data):
        if chunk_type == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            color_type = chunk[9]
            interlace = chunk[12]
        elif chunk_type == b"IDAT":
            idat.extend(chunk)
    if not width or not height:
        raise ValueError("PNG has no IHDR")
    if bit_depth != 8 or interlace != 0:
        raise ValueError("only 8-bit non-interlaced PNG screenshots are supported")
    channels = {0: 1, 2: 3, 6: 4}.get(color_type)
    if not channels:
        raise ValueError(f"unsupported PNG color type: {color_type}")

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    bpp = channels
    rows: list[bytes] = []
    pos = 0
    prev = bytearray(stride)
    for _ in range(height):
        filter_type = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + stride])
        pos += stride
        for i, value in enumerate(row):
            left = row[i - bpp] if i >= bpp else 0
            up = prev[i]
            upper_left = prev[i - bpp] if i >= bpp else 0
            if filter_type == 1:
                row[i] = (value + left) & 0xFF
            elif filter_type == 2:
                row[i] = (value + up) & 0xFF
            elif filter_type == 3:
                row[i] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                row[i] = (value + _paeth(left, up, upper_left)) & 0xFF
            elif filter_type != 0:
                raise ValueError(f"unsupported PNG filter: {filter_type}")
        rows.append(bytes(row))
        prev = row

    pixels: list[list[tuple[int, int, int]]] = []
    for row in rows:
        out = []
        for x in range(width):
            i = x * channels
            if color_type == 0:
                g = row[i]
                out.append((g, g, g))
            else:
                out.append((row[i], row[i + 1], row[i + 2]))
        pixels.append(out)
    return width, height, pixels


def _average_hash(pixels: list[list[tuple[int, int, int]]], width: int, height: int,
                  size: int = 8) -> str:
    values = []
    for yy in range(size):
        y = min(height - 1, int((yy + 0.5) * height / size))
        for xx in range(size):
            x = min(width - 1, int((xx + 0.5) * width / size))
            r, g, b = pixels[y][x]
            values.append((r * 299 + g * 587 + b * 114) // 1000)
    avg = sum(values) / len(values)
    bits = 0
    for value in values:
        bits = (bits << 1) | (1 if value >= avg else 0)
    return f"{bits:0{(size * size) // 4}x}"


def screen_signature(png: bytes) -> dict:
    sig = {
        "sha256": hashlib.sha256(png).hexdigest(),
        "bytes": len(png),
        "width": None,
        "height": None,
        "ahash": "",
        "ahash_bits": 64,
    }
    try:
        width, height, pixels = _decode_png_rgb(png)
        sig["width"] = width
        sig["height"] = height
        sig["ahash"] = _average_hash(pixels, width, height)
    except Exception as e:
        sig["signature_error"] = str(e)
    return sig


def signature_for_file(path: str) -> dict:
    with open(path, "rb") as f:
        return screen_signature(f.read())


def _hamming_hex(a: str, b: str) -> int:
    if not a or not b:
        return 9999
    try:
        width = max(len(a), len(b))
        return (int(a, 16) ^ int(b, 16)).bit_count() if len(a) == len(b) else (
            int(a.zfill(width), 16) ^ int(b.zfill(width), 16)).bit_count()
    except ValueError:
        return 9999


def match_rule(rule: dict, signature: dict) -> tuple[bool, str]:
    match = rule.get("match", {})
    if not isinstance(match, dict):
        return False, "missing match"
    if match.get("sha256") and match.get("sha256") == signature.get("sha256"):
        return True, "sha256"
    if match.get("width") and signature.get("width") and _safe_int(match["width"]) != _safe_int(signature["width"]):
        return False, "width mismatch"
    if match.get("height") and signature.get("height") and _safe_int(match["height"]) != _safe_int(signature["height"]):
        return False, "height mismatch"
    if match.get("ahash") and signature.get("ahash"):
        distance = _hamming_hex(str(match["ahash"]), str(signature["ahash"]))
        max_distance = _safe_int(match.get("max_distance", 0))
        if distance <= max_distance:
            return True, f"ahash distance {distance}/{max_distance}"
        return False, f"ahash distance {distance}/{max_distance}"
    return False, "no usable hash"


def _artifact_dir(job_id: str | None) -> str:
    name = job_id or datetime.now().strftime("fast-%Y%m%d-%H%M%S")
    path = os.path.join(ARTIFACTS_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _execute_action(action: dict, serial: str, emulator: str, package: str) -> tuple[bool, str]:
    kind = str(action.get("type", "")).strip()
    if kind == "tap":
        ok = adb.tap(serial, _safe_int(action.get("x")), _safe_int(action.get("y")), emulator)
        return ok, f"tap {action.get('x')},{action.get('y')}"
    if kind == "swipe":
        ok = adb.swipe(
            serial,
            _safe_int(action.get("x1")),
            _safe_int(action.get("y1")),
            _safe_int(action.get("x2")),
            _safe_int(action.get("y2")),
            _safe_int(action.get("ms"), 300),
            emulator,
        )
        return ok, "swipe"
    if kind == "wait":
        try:
            seconds = float(action.get("seconds", 0.6))
        except (TypeError, ValueError):
            seconds = 0.6
        time.sleep(max(0, seconds))
        return True, f"wait {seconds}s"
    if kind == "launch_app":
        ok = adb.launch_app(serial, str(action.get("package") or package), emulator)
        return ok, "launch_app"
    if kind == "stop_app":
        ok = adb.stop_app(serial, str(action.get("package") or package), emulator)
        return ok, "stop_app"
    if kind == "screenshot":
        return True, "screenshot"
    if kind == "report":
        return True, str(action.get("message", "report"))
    return False, f"unsupported action: {kind}"


def run_fast_rules(game: dict, task: str = "", job_id: str | None = None,
                   max_steps: int = 8) -> dict:
    result = {
        "enabled": game.get("control") == "emulator",
        "used": False,
        "completed": False,
        "rules_loaded": 0,
        "steps": [],
        "artifact_dir": "",
        "handoff_reason": "",
    }
    if not result["enabled"]:
        result["handoff_reason"] = "desktop control"
        return result

    game_id = str(game.get("id") or "")
    rules_data = load_rules(game_id)
    rules = [r for r in rules_data.get("rules", []) if r.get("enabled", True)]
    rules.sort(key=lambda r: int(r.get("priority", 0)), reverse=True)
    result["rules_loaded"] = len(rules)

    lc = game.get("launch", {})
    emulator = adb.normalize_emulator(lc.get("emulator", "ldplayer"))
    serial = lc.get("serial") or adb.serial_for(_safe_int(lc.get("instance", 0)), emulator)
    package = str(lc.get("package", "") or "")
    result["emulator"] = emulator
    result["serial"] = serial
    result["package"] = package

    if not adb.available(emulator):
        result["handoff_reason"] = f"{emulator} adb backend is unavailable"
        return result

    if not adb.adb_ready(serial, emulator):
        adb.launch_instance(_safe_int(lc.get("instance", 0)), emulator)
        time.sleep(6)
    if package:
        result["prelaunch"] = adb.launch_app(serial, package, emulator)
        time.sleep(2)

    artifact_dir = _artifact_dir(job_id)
    result["artifact_dir"] = artifact_dir
    last_signature = None
    for step_index in range(max(0, max_steps)):
        png = adb.screenshot(serial, emulator)
        if not png:
            result["handoff_reason"] = "screenshot failed"
            break
        screenshot_path = os.path.join(artifact_dir, f"fast_{step_index + 1:03d}.png")
        with open(screenshot_path, "wb") as f:
            f.write(png)
        signature = screen_signature(png)
        last_signature = signature
        result["last_screenshot"] = screenshot_path
        result["last_signature"] = signature

        matched = None
        match_reason = ""
        for rule in rules:
            ok, reason = match_rule(rule, signature)
            if ok:
                matched = rule
                match_reason = reason
                break
        if not matched:
            result["handoff_reason"] = "no matching fast rule"
            break

        result["used"] = True
        step = {
            "rule_id": matched.get("id", ""),
            "description": matched.get("description", ""),
            "match": match_reason,
            "screenshot": screenshot_path,
            "actions": [],
        }
        for action in matched.get("actions", []):
            if not isinstance(action, dict) or action.get("type") not in ALLOWED_ACTIONS:
                step["actions"].append({"ok": False, "detail": "invalid action"})
                result["steps"].append(step)
                result["handoff_reason"] = "invalid fast rule action"
                return result
            ok, detail = _execute_action(action, serial, emulator, package)
            step["actions"].append({"ok": ok, "detail": detail, "note": action.get("note", "")})
            if not ok:
                result["steps"].append(step)
                result["handoff_reason"] = f"action failed: {detail}"
                return result
            try:
                wait = float(action.get("wait", 0.7)) if action.get("type") not in ("wait", "screenshot", "report") else 0
            except (TypeError, ValueError):
                wait = 0.7
            if wait > 0:
                time.sleep(wait)
        after_png = adb.screenshot(serial, emulator)
        if after_png:
            after_path = os.path.join(artifact_dir, f"fast_{step_index + 1:03d}_after.png")
            with open(after_path, "wb") as f:
                f.write(after_png)
            after_signature = screen_signature(after_png)
            step["after_screenshot"] = after_path
            step["after_signature"] = after_signature
            result["last_screenshot"] = after_path
            result["last_signature"] = after_signature
        else:
            step["after_screenshot"] = ""
            result["handoff_reason"] = "post-action screenshot failed"
            result["steps"].append(step)
            return result
        result["steps"].append(step)
        if matched.get("complete"):
            result["completed"] = True
            result["handoff_reason"] = "completed by fast rule"
            break
        if matched.get("handoff"):
            result["handoff_reason"] = "handoff requested by fast rule"
            break

    if last_signature:
        result["last_signature"] = last_signature
    return result


def _normalize_action(action: dict) -> dict | None:
    if not isinstance(action, dict):
        return None
    kind = str(action.get("type", "")).strip()
    if kind not in ALLOWED_ACTIONS:
        return None
    clean: dict[str, Any] = {"type": kind}
    for key in ("x", "y", "x1", "y1", "x2", "y2", "ms"):
        if key in action:
            clean[key] = _safe_int(action.get(key))
    for key in ("seconds", "wait"):
        if key in action:
            try:
                clean[key] = float(action.get(key))
            except (TypeError, ValueError):
                pass
    for key in ("note", "message", "package"):
        if key in action and action.get(key) is not None:
            clean[key] = str(action.get(key))[:300]
    return clean


def _normalize_rule(rule: dict, source: str) -> dict | None:
    if not isinstance(rule, dict):
        return None
    match = rule.get("match")
    actions = rule.get("actions")
    if not isinstance(match, dict) or not isinstance(actions, list):
        return None
    if not match.get("sha256") and not match.get("ahash"):
        return None
    clean_actions = [_normalize_action(a) for a in actions]
    clean_actions = [a for a in clean_actions if a]
    if not clean_actions:
        return None
    rid = _slugify(str(rule.get("id") or rule.get("description") or "fast-rule"))
    clean_match = {}
    for key in ("sha256", "ahash"):
        if match.get(key):
            clean_match[key] = str(match[key])
    for key in ("width", "height", "max_distance"):
        if match.get(key) is not None:
            clean_match[key] = _safe_int(match[key])
    return {
        "id": rid,
        "description": str(rule.get("description") or rid)[:500],
        "enabled": bool(rule.get("enabled", True)),
        "priority": _safe_int(rule.get("priority", 0)),
        "match": clean_match,
        "actions": clean_actions,
        "complete": bool(rule.get("complete", False)),
        "handoff": bool(rule.get("handoff", False)),
        "source": source,
        "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def merge_rules(game_id: str, rules: list[dict], source: str = "codex") -> dict:
    data = load_rules(game_id)
    existing = {str(r.get("id")): r for r in data.get("rules", []) if isinstance(r, dict)}
    added = 0
    updated = 0
    for rule in rules:
        clean = _normalize_rule(rule, source)
        if not clean:
            continue
        if clean["id"] in existing:
            existing[clean["id"]].update(clean)
            updated += 1
        else:
            existing[clean["id"]] = clean
            added += 1
    data["rules"] = list(existing.values())
    if added or updated:
        save_rules(game_id, data)
    return {"added": added, "updated": updated, "total": len(data["rules"]), "path": _rules_path(game_id)}


def extract_rule_block(text: str) -> list[dict]:
    marker = "AUTOGAMETEST_FAST_RULES"
    idx = text.find(marker)
    if idx < 0:
        return []
    tail = text[idx + len(marker):].lstrip(" :\n\r\t")
    if tail.startswith("```"):
        first_newline = tail.find("\n")
        if first_newline >= 0:
            tail = tail[first_newline + 1:]
        end = tail.find("```")
        if end >= 0:
            tail = tail[:end]
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(tail.strip())
    except json.JSONDecodeError:
        return []
    if isinstance(obj, dict):
        obj = obj.get("rules", [obj] if "actions" in obj else [])
    return obj if isinstance(obj, list) else []


def rules_summary(game_id: str, limit: int = 12) -> str:
    data = load_rules(game_id)
    rules = data.get("rules", [])
    if not rules:
        return "目前沒有快速規則。"
    rows = []
    for rule in rules[:limit]:
        action_count = len(rule.get("actions", []))
        rows.append(
            f"- {rule.get('id')}: {rule.get('description', '')} "
            f"({action_count} actions, enabled={rule.get('enabled', True)})")
    if len(rules) > limit:
        rows.append(f"- ... 還有 {len(rules) - limit} 條")
    return "\n".join(rows)


def format_fast_context(game_id: str, fast_result: dict | None) -> str:
    if not fast_result or not fast_result.get("enabled"):
        return ""
    steps = fast_result.get("steps", [])
    lines = [
        "# 快速判斷層（本地規則）",
        "系統已先用本地快速規則嘗試處理可辨識畫面；未知畫面才交給你判斷。",
        f"已載入規則數：{fast_result.get('rules_loaded', 0)}",
        f"已執行規則數：{len(steps)}",
        f"交接原因：{fast_result.get('handoff_reason', '')}",
    ]
    if fast_result.get("last_screenshot"):
        lines.append(f"目前截圖：`{fast_result['last_screenshot']}`")
    if fast_result.get("last_signature"):
        sig = fast_result["last_signature"]
        lines.append(
            "目前畫面 signature："
            f"sha256={sig.get('sha256')} ahash={sig.get('ahash')} "
            f"size={sig.get('width')}x{sig.get('height')}")
    if steps:
        lines.append("已執行：")
        for step in steps:
            lines.append(f"- {step.get('rule_id')}: {step.get('description')} [{step.get('match')}]")
    lines.extend([
        "",
        "已知快速規則：",
        rules_summary(game_id),
        "",
        "若你在本次操作中確認某個安全、低風險、可重複的單畫面動作，請在最終回報最後附上：",
        "AUTOGAMETEST_FAST_RULES:",
        "```json",
        "[",
        "  {",
        '    "id": "close-known-popup",',
        '    "description": "關閉已確認的安全彈窗",',
        '    "match": {"sha256": "<截圖sha256>", "ahash": "<截圖ahash>", "width": 1280, "height": 720, "max_distance": 0},',
        '    "actions": [{"type": "tap", "x": 1000, "y": 120, "wait": 0.8, "note": "close"}],',
        '    "complete": false',
        '    "handoff": false',
        "  }",
        "]",
        "```",
        "只學習登入、付費、轉蛋、PVP 以外的安全選單操作。遇到高風險畫面必須停止回報。",
        "可用 `python tools/fast_rules.py signature <png>` 取得截圖 signature。",
    ])
    return "\n".join(lines)
