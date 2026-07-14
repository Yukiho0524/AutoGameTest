"""Visual memory store for game testing.

Visual memory is a lightweight catalog of screenshots and UI observations. It
keeps image paths, signatures, labels, regions, and safe action hints separate
from SKILL.md so the skill stays readable while agents still get visual context.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from typing import Any

from . import fast_agent

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VISUAL_MEMORY_DIR = os.path.join(ROOT, "data", "visual_memory")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "game"


def _game_dir(game_id: str) -> str:
    return os.path.join(VISUAL_MEMORY_DIR, _slugify(game_id))


def memory_path(game_id: str) -> str:
    return os.path.join(_game_dir(game_id), "memory.json")


def images_dir(game_id: str) -> str:
    return os.path.join(_game_dir(game_id), "images")


def _abs_path(path: str) -> str:
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))


def _rel_path(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace("\\", "/")
    except ValueError:
        return path


def load_memory(game_id: str) -> dict:
    path = memory_path(game_id)
    if not os.path.isfile(path):
        return {"version": 1, "game_id": game_id, "images": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "game_id": game_id, "images": []}
    if not isinstance(data, dict):
        return {"version": 1, "game_id": game_id, "images": []}
    data.setdefault("version", 1)
    data.setdefault("game_id", game_id)
    data.setdefault("images", [])
    if not isinstance(data["images"], list):
        data["images"] = []
    return data


def save_memory(game_id: str, data: dict) -> None:
    os.makedirs(_game_dir(game_id), exist_ok=True)
    data["version"] = 1
    data["game_id"] = game_id
    tmp = memory_path(game_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, memory_path(game_id))


def _file_signature(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read()
    sig = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "width": None,
        "height": None,
        "ahash": "",
    }
    if data.startswith(fast_agent.PNG_SIGNATURE):
        sig.update(fast_agent.screen_signature(data))
    return sig


def _clean_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = [x.strip() for x in re.split(r"[,，\n]", tags) if x.strip()]
    if not isinstance(tags, list):
        return []
    clean = []
    for tag in tags:
        value = str(tag).strip()
        if value and value not in clean:
            clean.append(value[:50])
    return clean[:20]


def _clean_regions(regions: Any) -> list[dict]:
    if not isinstance(regions, list):
        return []
    clean = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        item = {}
        for key in ("name", "note", "risk"):
            if region.get(key) is not None:
                item[key] = str(region[key])[:200]
        for key in ("x", "y", "w", "h"):
            if region.get(key) is not None:
                try:
                    item[key] = int(region[key])
                except (TypeError, ValueError):
                    pass
        if item:
            clean.append(item)
    return clean[:30]


def _clean_actions(actions: Any) -> list[dict]:
    if not isinstance(actions, list):
        return []
    clean = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        item = {}
        for key in ("type", "note", "risk", "message", "package"):
            if action.get(key) is not None:
                item[key] = str(action[key])[:200]
        for key in ("x", "y", "x1", "y1", "x2", "y2", "ms"):
            if action.get(key) is not None:
                try:
                    item[key] = int(action[key])
                except (TypeError, ValueError):
                    pass
        for key in ("seconds", "wait"):
            if action.get(key) is not None:
                try:
                    item[key] = float(action[key])
                except (TypeError, ValueError):
                    pass
        if item:
            clean.append(item)
    return clean[:30]


def _clean_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
    return default


def _clean_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_rule_hints(raw: dict) -> dict:
    hints = {}
    for key in ("complete", "handoff", "fast_match"):
        if key in raw:
            hints[key] = _clean_bool(raw.get(key), False)
    for key in ("priority", "max_repeats", "fast_max_distance", "max_distance"):
        if raw.get(key) is not None:
            hints[key] = _clean_int(raw.get(key), 0)
    return hints


def _entry_id(label: str, signature: dict) -> str:
    base = _slugify(label or "screen")
    sha = str(signature.get("sha256") or uuid.uuid4().hex)
    return f"{base}-{sha[:8]}"


def remember_image(game_id: str, image_path: str, label: str = "",
                   note: str = "", tags: Any = None, state: str = "",
                   regions: Any = None, actions: Any = None,
                   risk: str = "safe", source: str = "manual",
                   copy_image: bool = True, complete: bool = False,
                   handoff: bool = False, priority: int | None = None,
                   fast_match: bool = False,
                   fast_max_distance: int | None = None,
                   max_repeats: int | None = None) -> dict:
    full = _abs_path(image_path)
    if not os.path.isfile(full):
        raise FileNotFoundError(full)
    if os.path.splitext(full)[1].lower() not in IMAGE_EXTS:
        raise ValueError(f"unsupported image type: {full}")

    signature = _file_signature(full)
    label = (label or state or os.path.splitext(os.path.basename(full))[0]).strip()
    entry_id = _entry_id(label, signature)

    stored_path = full
    if copy_image:
        os.makedirs(images_dir(game_id), exist_ok=True)
        ext = os.path.splitext(full)[1].lower() or ".png"
        stored_path = os.path.join(images_dir(game_id), f"{entry_id}{ext}")
        if os.path.abspath(full) != os.path.abspath(stored_path):
            shutil.copy2(full, stored_path)

    entry = {
        "id": entry_id,
        "label": label[:120],
        "state": str(state or label)[:120],
        "note": str(note or "")[:1000],
        "tags": _clean_tags(tags),
        "risk": str(risk or "safe")[:80],
        "image_path": _rel_path(stored_path),
        "signature": signature,
        "regions": _clean_regions(regions),
        "actions": _clean_actions(actions),
        "source": source,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    entry.update(_clean_rule_hints({
        "complete": complete,
        "handoff": handoff,
        "priority": priority,
        "fast_match": fast_match,
        "fast_max_distance": fast_max_distance,
        "max_repeats": max_repeats,
    }))
    data = load_memory(game_id)
    images = data.get("images", [])
    updated = False
    for index, old in enumerate(images):
        old_sig = (old or {}).get("signature", {})
        if old.get("id") == entry_id or old_sig.get("sha256") == signature.get("sha256"):
            merged = {**old, **entry}
            images[index] = merged
            entry = merged
            updated = True
            break
    if not updated:
        entry["created_at"] = entry["updated_at"]
        images.append(entry)
    data["images"] = images
    save_memory(game_id, data)
    return {"entry": entry, "updated": updated, "path": memory_path(game_id)}


def merge_entries(game_id: str, entries: list[dict], source: str = "codex-output") -> dict:
    data = load_memory(game_id)
    images = {str(e.get("id")): e for e in data.get("images", []) if isinstance(e, dict)}
    added = 0
    updated = 0
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        image_path = raw.get("image_path") or raw.get("path")
        if image_path:
            try:
                result = remember_image(
                    game_id,
                    str(image_path),
                    label=str(raw.get("label") or raw.get("state") or ""),
                    note=str(raw.get("note") or raw.get("description") or ""),
                    tags=raw.get("tags"),
                    state=str(raw.get("state") or ""),
                    regions=raw.get("regions"),
                    actions=raw.get("actions"),
                    risk=str(raw.get("risk") or "safe"),
                    source=source,
                    copy_image=True,
                    complete=_clean_bool(raw.get("complete"), False),
                    handoff=_clean_bool(raw.get("handoff"), False),
                    priority=raw.get("priority"),
                    fast_match=_clean_bool(raw.get("fast_match"), False),
                    fast_max_distance=raw.get("fast_max_distance", raw.get("max_distance")),
                    max_repeats=raw.get("max_repeats"),
                )
                if result["updated"]:
                    updated += 1
                else:
                    added += 1
                images = {
                    str(e.get("id")): e
                    for e in load_memory(game_id).get("images", [])
                    if isinstance(e, dict)
                }
            except (OSError, ValueError):
                continue
            continue

        signature = raw.get("signature")
        if not isinstance(signature, dict) or not signature.get("sha256"):
            continue
        label = str(raw.get("label") or raw.get("state") or "screen")[:120]
        entry = {
            "id": str(raw.get("id") or _entry_id(label, signature)),
            "label": label,
            "state": str(raw.get("state") or label)[:120],
            "note": str(raw.get("note") or raw.get("description") or "")[:1000],
            "tags": _clean_tags(raw.get("tags")),
            "risk": str(raw.get("risk") or "safe")[:80],
            "image_path": str(raw.get("image_path") or ""),
            "signature": signature,
            "regions": _clean_regions(raw.get("regions")),
            "actions": _clean_actions(raw.get("actions")),
            "source": source,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        entry.update(_clean_rule_hints(raw))
        if entry["id"] in images:
            images[entry["id"]].update(entry)
            updated += 1
        else:
            entry["created_at"] = entry["updated_at"]
            images[entry["id"]] = entry
            added += 1
    if added or updated:
        data = load_memory(game_id)
        data["images"] = list(images.values())
        save_memory(game_id, data)
    fast_rules = promote_safe_rules(game_id) if added or updated else {
        "added": 0, "updated": 0, "total": 0, "path": "",
        "candidates": 0,
    }
    return {
        "added": added,
        "updated": updated,
        "total": len(load_memory(game_id).get("images", [])),
        "path": memory_path(game_id),
        "fast_rules": fast_rules,
    }


def promote_safe_rules(game_id: str) -> dict:
    """Promote actionable safe visual memories into explicit fast rules."""
    rules = fast_agent.load_visual_memory_rules(game_id)
    if not rules:
        return {
            "added": 0,
            "updated": 0,
            "total": len(fast_agent.load_rules(game_id).get("rules", [])),
            "path": fast_agent._rules_path(game_id),
            "candidates": 0,
        }
    merged = fast_agent.merge_rules(
        game_id, rules, source="visual-memory-promoted")
    merged["candidates"] = len(rules)
    return merged


def extract_memory_block(text: str) -> list[dict]:
    marker = "AUTOGAMETEST_VISUAL_MEMORY"
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
        obj = obj.get("images", [obj] if "signature" in obj or "image_path" in obj else [])
    return obj if isinstance(obj, list) else []


def summary(game_id: str, limit: int = 20) -> str:
    images = load_memory(game_id).get("images", [])
    if not images:
        return "目前沒有圖片記憶。"
    rows = []
    for item in images[:limit]:
        sig = item.get("signature", {})
        tags = ", ".join(item.get("tags", [])[:5])
        rows.append(
            f"- {item.get('id')}: {item.get('label')} / {item.get('state')} "
            f"risk={item.get('risk', 'safe')} tags=[{tags}] "
            f"image={item.get('image_path', '')} "
            f"sha256={str(sig.get('sha256', ''))[:12]} ahash={sig.get('ahash', '')} "
            f"note={item.get('note', '')[:160]}")
    if len(images) > limit:
        rows.append(f"- ... 還有 {len(images) - limit} 筆")
    return "\n".join(rows)


def format_prompt_context(game_id: str, limit: int = 20) -> str:
    data = load_memory(game_id)
    images = data.get("images", [])
    if not images:
        return "# 圖片記憶\n目前沒有圖片記憶。"
    def _rank(item: dict) -> tuple:
        actions = item.get("actions") or []
        risk = str(item.get("risk") or "").lower()
        safe = 1 if risk in {"safe", "low", "routine"} else 0
        try:
            priority = int(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return (
            safe,
            1 if actions else 0,
            priority,
            str(item.get("updated_at") or item.get("created_at") or ""),
        )
    images = sorted(
        [item for item in images if isinstance(item, dict)],
        key=_rank,
        reverse=True,
    )
    lines = [
        "# 圖片記憶",
        "以下是已知遊戲畫面的截圖記憶。請用它輔助判斷 UI 狀態、可點區域與風險；不要把登入、付款、轉蛋、PVP 當成可自動化安全操作。",
    ]
    for item in images[:limit]:
        sig = item.get("signature", {})
        lines.extend([
            f"## {item.get('label')} ({item.get('id')})",
            f"- 狀態：{item.get('state', '')}",
            f"- 風險：{item.get('risk', 'safe')}",
            f"- 標籤：{', '.join(item.get('tags', []))}",
            f"- 圖片：`{item.get('image_path', '')}`",
            f"- signature：sha256={sig.get('sha256', '')} ahash={sig.get('ahash', '')} size={sig.get('width')}x{sig.get('height')}",
            f"- 記憶：{item.get('note', '')}",
        ])
        regions = item.get("regions") or []
        if regions:
            lines.append("- 區域：" + json.dumps(regions[:8], ensure_ascii=False))
        actions = item.get("actions") or []
        if actions:
            lines.append("- 已知動作：" + json.dumps(actions[:8], ensure_ascii=False))
    if len(images) > limit:
        lines.append(f"- 圖片記憶尚有 {len(images) - limit} 筆未列出。")
    lines.extend([
        "",
        "若本次操作新確認某張截圖代表的 UI 狀態，請在最終回報最後附上：",
        "AUTOGAMETEST_VISUAL_MEMORY:",
        "```json",
        "[",
        "  {",
        '    "image_path": "data/artifacts/<job_id>/fast_001.png",',
        '    "label": "主畫面",',
        '    "state": "home",',
        '    "note": "可從此畫面進入任務/活動/信箱。",',
        '    "tags": ["home", "safe"],',
        '    "risk": "safe",',
        '    "fast_match": true,',
        '    "fast_max_distance": 2,',
        '    "priority": 10,',
        '    "max_repeats": 1,',
        '    "complete": false,',
        '    "handoff": false,',
        '    "regions": [{"name": "任務", "x": 1000, "y": 620, "w": 120, "h": 80, "note": "進入任務"}],',
        '    "actions": [{"type": "tap", "x": 1000, "y": 620, "wait": 0.8, "note": "打開任務"}]',
        "  }",
        "]",
        "```",
        "只有 safe / low / routine 且帶安全 actions 的圖片記憶會自動晉升為 fast rules；登入、付款、轉蛋、PVP 只能記風險，不可給安全自動動作。",
    ])
    return "\n".join(lines)
