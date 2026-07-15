"""Post-run performance autotune for AutoGameTest agents.

This runner is intentionally conservative: it feeds a completed run_agent job's
performance diagnosis back to Codex, but asks Codex to adjust only project
knowledge (skill/agent notes and already-safe local rules), not to operate the
game or rewrite broad application code.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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

from core import store, fast_agent, visual_memory  # noqa: E402
import ai_runner  # noqa: E402


def _clip(value, limit: int = 5000) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _read_rel(path_rel: str) -> str:
    path = os.path.join(ROOT, path_rel or "")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _image_candidate(path: str, source: str, detail: dict | None = None) -> dict | None:
    if not path:
        return None
    full = _abs(str(path))
    if not os.path.isfile(full):
        return None
    if os.path.splitext(full)[1].lower() not in visual_memory.IMAGE_EXTS:
        return None
    item = {
        "image_path": _rel(full),
        "source": source,
    }
    if detail:
        for key, value in detail.items():
            if value not in (None, "", [], {}):
                item[key] = value
    return item


def _compact_decision(decision: dict | None) -> dict:
    if not isinstance(decision, dict):
        return {}
    keep = {}
    for key in (
        "status", "action", "x", "y", "x1", "y1", "x2", "y2", "ms",
        "seconds", "reason", "next_state",
    ):
        value = decision.get(key)
        if value not in (None, "", [], {}):
            keep[key] = value
    for key in ("reason", "next_state"):
        if key in keep:
            keep[key] = str(keep[key])[:260]
    return keep


def _candidate_score(item: dict) -> tuple:
    detail_text = " ".join(
        str(item.get(k) or "").lower()
        for k in ("source", "status", "action", "reason", "observation",
                  "learned", "next_state", "execute_detail")
    )
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    source = str(item.get("source") or "")
    action = str(item.get("action") or decision.get("action") or "").lower()
    status = str(item.get("status") or "").lower()
    score = 0
    if source == "visual_turn":
        score += 30
    if source.startswith("fast_rule"):
        score += 25
    if status == "done":
        score += 20
    if status in {"error", "stopped", "timeout"}:
        score += 10
    if action in {"tap", "swipe", "wait", "back", "launch_app"}:
        score += 18
    if decision.get("x") is not None and decision.get("y") is not None:
        score += 20
    if any(word in detail_text for word in (
        "主畫面", "home", "title", "tap to start", "確認", "執行",
        "領取", "完成", "complete", "任務", "關卡", "stage", "skip",
        "略過", "獎勵", "reward", "彈窗", "dialog", "loading", "載入"
    )):
        score += 12
    if any(word in detail_text for word in (
        "登入", "授權", "付費", "購買", "儲值", "抽卡", "轉蛋",
        "pvp", "rank", "排位", "對戰"
    )):
        score -= 30
    try:
        turn = int(item.get("turn") or 0)
    except (TypeError, ValueError):
        turn = 0
    return score, turn


def collect_visual_candidates(source_job: dict, limit: int = 18) -> list[dict]:
    """Collect existing artifact screenshots worth asking autotune to review."""
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(path: str, source: str, detail: dict | None = None) -> None:
        item = _image_candidate(path, source, detail)
        if not item:
            return
        key = item["image_path"]
        if key in seen:
            return
        seen.add(key)
        candidates.append(item)

    performance = source_job.get("performance") or {}
    for turn in performance.get("visual_turns") or []:
        if not isinstance(turn, dict):
            continue
        add(turn.get("screenshot", ""), "visual_turn", {
            "turn": turn.get("turn"),
            "status": turn.get("status"),
            "action": turn.get("action"),
            "execute_detail": str(turn.get("execute_detail") or "")[:160],
            "reason": str(turn.get("reason") or "")[:300],
            "observation": str(turn.get("observation") or "")[:500],
            "learned": str(turn.get("learned") or "")[:500],
            "next_state": str(turn.get("next_state") or "")[:300],
            "decision": _compact_decision(turn.get("decision")),
        })

    fast_decision = source_job.get("fast_decision") or {}
    for step in fast_decision.get("steps") or []:
        if not isinstance(step, dict):
            continue
        detail = {
            "rule_id": step.get("rule_id"),
            "description": str(step.get("description") or "")[:300],
            "match": str(step.get("match") or "")[:300],
            "actions": step.get("actions") or [],
        }
        add(step.get("screenshot", ""), "fast_rule_before", detail)
        add(step.get("after_screenshot", ""), "fast_rule_after", detail)
    add(fast_decision.get("last_screenshot", ""), "fast_layer_last", {
        "handoff_reason": fast_decision.get("handoff_reason", ""),
        "completed": fast_decision.get("completed"),
        "used": fast_decision.get("used"),
    })

    artifact_dir = performance.get("artifact_dir") or fast_decision.get("artifact_dir")
    if artifact_dir:
        full_dir = _abs(str(artifact_dir))
        if os.path.isdir(full_dir):
            for name in sorted(os.listdir(full_dir)):
                if os.path.splitext(name)[1].lower() not in visual_memory.IMAGE_EXTS:
                    continue
                add(os.path.join(full_dir, name), "artifact_dir", {
                    "artifact_dir": _rel(full_dir),
                })
    candidates.sort(key=_candidate_score, reverse=True)
    return candidates[:limit]


def _short_text(value, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def collect_turn_flow_summary(source_job: dict, limit: int = 80) -> list[dict]:
    performance = source_job.get("performance") or {}
    turns = performance.get("visual_turns") or []
    rows: list[dict] = []
    if not isinstance(turns, list):
        return rows
    for turn in turns[:limit]:
        if not isinstance(turn, dict):
            continue
        decision = turn.get("decision") if isinstance(turn.get("decision"), dict) else {}
        rows.append({
            "turn": turn.get("turn"),
            "status": turn.get("status"),
            "action": turn.get("action") or decision.get("action"),
            "x": decision.get("x"),
            "y": decision.get("y"),
            "reason": _short_text(turn.get("reason") or decision.get("reason"), 180),
            "observation": _short_text(
                turn.get("observation") or decision.get("observation"), 220),
            "learned": _short_text(turn.get("learned") or decision.get("learned"), 220),
            "next_state": _short_text(
                turn.get("next_state") or decision.get("next_state"), 160),
        })
    return rows


def _git_status() -> str:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
        )
        return proc.stdout.strip()
    except Exception as e:
        return f"git status unavailable: {e}"


def _extract_marked_json(text: str, marker: str):
    idx = (text or "").find(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker):].lstrip(" :\n\r\t")
    if tail.startswith("```"):
        first_newline = tail.find("\n")
        if first_newline >= 0:
            tail = tail[first_newline + 1:]
        end = tail.find("```")
        if end >= 0:
            tail = tail[:end]
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail.strip())
        return obj
    except json.JSONDecodeError:
        return tail.strip()


def extract_skill_lessons(text: str) -> list[str]:
    obj = _extract_marked_json(text or "", "AUTOGAMETEST_SKILL_LESSONS")
    if obj is None:
        summary = _extract_marked_json(text or "", "AUTOGAMETEST_AUTOTUNE_SUMMARY")
        if isinstance(summary, dict):
            obj = summary.get("skill_lessons") or summary.get("lessons")
    if obj is None:
        return []
    if isinstance(obj, dict):
        obj = obj.get("lessons") or obj.get("items") or [obj]
    if isinstance(obj, str):
        obj = [line for line in obj.splitlines() if line.strip()]
    if not isinstance(obj, list):
        return []
    lessons = []
    for item in obj[:12]:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = (
                item.get("lesson")
                or item.get("text")
                or item.get("note")
                or item.get("summary")
                or "；".join(f"{k}: {v}" for k, v in item.items() if v)
            )
        else:
            continue
        text = " ".join(str(text or "").split()).strip(" -")
        if text and text not in lessons:
            lessons.append(text[:700])
    return lessons


def extract_autotune_summary(text: str) -> dict | None:
    obj = _extract_marked_json(text or "", "AUTOGAMETEST_AUTOTUNE_SUMMARY")
    return obj if isinstance(obj, dict) else None


def extract_game_understanding(text: str) -> dict | None:
    obj = _extract_marked_json(text or "", "AUTOGAMETEST_GAME_UNDERSTANDING")
    return obj if isinstance(obj, dict) else None


def _list_lines(title: str, values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    lines = [f"### {title}", ""]
    for item in values[:12]:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("title") or item.get("state") or ""
            detail = (
                item.get("detail")
                or item.get("note")
                or item.get("next")
                or item.get("rule")
                or item.get("summary")
                or ""
            )
            text = f"{name}: {detail}" if name and detail else (name or detail)
        else:
            continue
        text = " ".join(str(text or "").split()).strip(" -")
        if text:
            lines.append(f"- {text[:500]}")
    lines.append("")
    return lines


def format_game_understanding(data: dict) -> str:
    lines: list[str] = []
    overview = str(data.get("overview") or data.get("summary") or "").strip()
    if overview:
        lines.extend(["### 核心理解", "", overview[:1200], ""])
    lines.extend(_list_lines("主線與新手流程", data.get("mainline_flow") or data.get("flow")))
    lines.extend(_list_lines("UI 地圖與入口", data.get("ui_map") or data.get("screens")))
    lines.extend(_list_lines("核心機制", data.get("mechanics")))
    lines.extend(_list_lines("安全操作策略", data.get("safe_strategies") or data.get("safe_actions")))
    lines.extend(_list_lines("風險與停止條件", data.get("risks") or data.get("stop_conditions")))
    lines.extend(_list_lines("已知卡點與處理", data.get("frictions") or data.get("stuck_points")))
    lines.extend(_list_lines("完成判定", data.get("completion_signals") or data.get("done_signals")))
    body = "\n".join(lines).strip()
    return body[:6000]


def build_autotune_prompt(tune_job: dict, source_job: dict) -> str:
    payload = tune_job.get("payload", {}) if isinstance(tune_job, dict) else {}
    game_id = payload.get("game_id") or source_job.get("payload", {}).get("game_id", "")
    agent_id = payload.get("agent_id") or source_job.get("payload", {}).get("agent_id", "")
    game = store.get_game(game_id) or {}
    agent = store.get_agent(agent_id) or {}
    skill_path = game.get("skill_path", f".codex/skills/{game_id}/SKILL.md")
    agent_path = game.get("agent_path", f".codex/agents/{game_id}-player.md")
    allowed_paths = [
        skill_path,
        agent_path,
        os.path.relpath(fast_agent._rules_path(game_id), ROOT).replace(os.sep, "/"),
        os.path.relpath(visual_memory.memory_path(game_id), ROOT).replace(os.sep, "/"),
    ]
    analysis = source_job.get("performance_analysis") or source_job.get("performance", {}).get("analysis") or {}
    performance = source_job.get("performance") or {}
    fast_decision = source_job.get("fast_decision") or {}
    result = source_job.get("result") or ""
    visual_candidates = collect_visual_candidates(source_job)
    turn_flow = collect_turn_flow_summary(source_job)
    current_visual_memory = visual_memory.summary(game_id, limit=12)
    skill = _read_rel(skill_path)
    agent_text = _read_rel(agent_path)
    current_status = _git_status()
    return f"""你是 AutoGameTest 的「Agent 效能調整器」與「遊戲理解整理器」。請根據一次已完成的 Agent 執行結果與效能診斷，產出**最小、保守、可回滾**的知識調整建議，目標是讓同一遊戲下次判斷更快、更少重複分析，並逐步建立對遊戲流程與機制的完整理解。

# 執行方式
- 這個 autotune 任務可能在 read-only sandbox 中執行，這是正常狀態，不代表失敗。
- 不要嘗試直接修改檔案、不要呼叫 shell 寫檔、不要使用 apply_patch。
- 你只需要輸出下方指定的 `AUTOGAMETEST_*` 結構化區塊；Python runner 會負責真正落檔與合併。
- 若環境顯示 read-only，仍請照常輸出可被 runner 套用的 `AUTOGAMETEST_GAME_UNDERSTANDING` / `AUTOGAMETEST_SKILL_LESSONS` / `AUTOGAMETEST_VISUAL_MEMORY` / `AUTOGAMETEST_AUTOTUNE_SUMMARY`。

# 絕對邊界
- 不要操作遊戲、不跑 adb、不截圖、不登入、不購買、不抽卡、不進 PVP。
- 不要修改通用應用程式碼，除非效能診斷明確指出是程式 bug；本任務預設只輸出知識檔調整建議。
- 不要 git commit / git push。
- 不要覆蓋使用者未提交修改；若檔案已有內容，只追加或小幅整理相關段落。
- 不要根據猜測創建 fast rule。只有在 source job 已明確包含安全 `fast_rules` 或截圖 signature + 安全動作時，才可補齊安全本地規則。
- 登入、付費、購買、抽卡/轉蛋、PVP/排位相關畫面只能寫成風險教訓，禁止建立可自動點擊規則。
- 可以評估 source job 已存在的 artifact 截圖是否值得加入圖片記憶；不要自行截圖或操作遊戲。

# 可調整檔案
{json.dumps(allowed_paths, ensure_ascii=False, indent=2)}

# 建議調整方向
- 這個 autotune 是跨遊戲的持續優化迴圈：每次 Agent 跑完，都應盡量把「下次可少問 AI」的穩定知識沉澱下來。
- 若 source job 是自主探索或長時間體驗，除了效能優化，也要整理「這遊戲怎麼玩」：主線階段、主要 UI、核心機制、玩家卡點、完成判定、風險入口。這些輸出到 `AUTOGAMETEST_GAME_UNDERSTANDING`，不要只寫成零碎 lesson。
- 優先整理成三種可重用知識：
  1. 畫面狀態：這張圖代表哪個 UI 狀態、如何辨識、是否安全。
  2. 下一步動作：若畫面安全且任務仍需推進，下一個穩定動作是什麼。
  3. 完成/停止判定：看到哪些文字或 UI 代表本任務階段完成，不要重做前面步驟。
- 如果效能建議提到 fast layer 沒命中：在 Skill/Agent 中加入「遇到已知安全畫面要輸出 AUTOGAMETEST_FAST_RULES 或 VISUAL_MEMORY」的精煉教訓；不要憑空造座標。
- 如果 Codex 判斷耗時很長：把可重複的完成判定、已知畫面狀態、停止條件寫進 Skill，讓下次少推理。
- 如果分段任務後段重複確認相同畫面：寫入「畫面已符合前序步驟時直接承認完成，不退回重做」的遊戲專屬規則。
- 如果 prompt/skill 太長：只整理該遊戲 Skill 中重複的經驗教訓，保留安全邊界。
- 檢查「圖片記憶候選截圖」：穩定、可辨識、未重複且未來會再次出現的畫面，請輸出 `AUTOGAMETEST_VISUAL_MEMORY`。
- 如果候選截圖帶有 `decision` 的 x/y 且該動作已在 source job 成功執行，可在 safe/low/routine 畫面中保守附上 actions；runner 會自動晉升 fast rules。
- 對同一流程連續出現的畫面，優先保留「入口、確認、結果/完成」三類截圖，而不是把每個過場都加入記憶。
- 對登入、付費、抽卡、PVP、未知高風險畫面，只能建立 `risk: "high"` / `"manual"` / `"pvp"` 等風險記憶，不要給 actions、fast_match 或 complete。
- 對主畫面、一般選單、完成畫面、loading、已知安全彈窗，可建立 safe/low/routine 記憶；只有安全且可重複的畫面才附 actions。
- 若沒有足夠資訊安全調整，請不要硬輸出 lesson 或圖片記憶，只在 summary 說明原因。

# 遊戲與 Agent
game_id: {game_id}
game_name: {game.get('name', '')}
agent_id: {agent_id}
agent_name: {agent.get('name', '')}

# 目前 git status（請尊重既有未提交變更）
```text
{current_status or '(clean)'}
```

# 效能診斷 analysis
```json
{_clip(analysis, 7000)}
```

# performance 摘要
```json
{_clip(performance, 9000)}
```

# 自主探索/逐圖全流程摘要
這份摘要比原始 result 更重要：它保留每一輪的畫面觀察、動作、學到的規則與下一狀態，請用它整理遊戲理解。
```json
{_clip(turn_flow, 18000)}
```

# fast_decision 摘要
```json
{_clip(fast_decision, 7000)}
```

# 圖片記憶候選截圖
以下都是 source job 已保存的截圖路徑，已依可重用性排序；可用這些 `image_path` 建立 visual memory。若候選沒有可重用價值，請不要硬加。
候選中的 `decision` 是當時 AI 對該截圖採取的動作摘要；只有確認安全、低風險、且 action 已成功推進流程時，才可轉成 actions。
```json
{_clip(visual_candidates, 9000)}
```

# 目前圖片記憶摘要
```text
{_clip(current_visual_memory, 5000)}
```

# 原任務 result 摘要
```text
{_clip(result, 7000)}
```

# 目前 Skill 摘要
檔案：`{skill_path}`
```markdown
{_clip(skill, 7000) or '(missing)'}
```

# 目前 Agent 摘要
檔案：`{agent_path}`
```markdown
{_clip(agent_text, 5000) or '(missing)'}
```

請只輸出必要的最小結構化調整。
如果本次執行足以補強對遊戲的完整理解，請輸出：
AUTOGAMETEST_GAME_UNDERSTANDING:
```json
{{
  "overview": "用 2-5 句整理目前已知的遊戲玩法、核心循環、玩家目標。",
  "mainline_flow": [
    "可重複的主線/新手流程，例如：標題 -> 進入遊戲 -> 任務卡 -> 商品/布置/貨架 -> 領獎。"
  ],
  "ui_map": [
    {{"name": "主店鋪", "detail": "重要入口、任務卡、貨幣、手機/便利机、商品管理、布置等位置與用途。"}}
  ],
  "mechanics": [
    "已理解的遊戲機制，例如貨架容量、商品上架、進貨、任務獎勵、教學對話。"
  ],
  "safe_strategies": [
    "下次遇到同類畫面時可採用的安全策略，需可重用。"
  ],
  "risks": [
    "登入、付費、抽卡、回收庫存、消耗鑽石/付費幣等停止或深判條件。"
  ],
  "frictions": [
    "玩家或 agent 容易卡住的地方，以及保守處理方式。"
  ],
  "completion_signals": [
    "哪些文字/UI 代表任務階段完成，避免重做。"
  ]
}}
```

如果有可寫入 Skill「經驗教訓」段的可重用規則，請輸出：
AUTOGAMETEST_SKILL_LESSONS:
```json
[
  "可重用、遊戲專屬、能讓下次更快判斷的教訓；格式建議包含：畫面/狀態 -> 安全下一步/完成判定；不要寫一次性流水帳。"
]
```

如果評估出可加入圖片記憶的截圖，請輸出：
AUTOGAMETEST_VISUAL_MEMORY:
```json
[
  {{
    "image_path": "data/artifacts/<job_id>/visual_001.png",
    "label": "主畫面",
    "state": "home",
    "note": "可辨識的 UI 狀態與下次如何判斷。",
    "tags": ["home", "safe"],
    "risk": "safe",
    "fast_match": true,
    "fast_max_distance": 2,
    "priority": 10,
    "max_repeats": 1,
    "regions": [{{"name": "任務入口", "x": 1000, "y": 620, "w": 120, "h": 80, "note": "安全入口"}}],
    "actions": [{{"type": "tap", "x": 1000, "y": 620, "wait": 0.8, "note": "打開任務"}}]
  }}
]
```
若畫面只適合辨識而不適合自動操作，請不要附 actions；若畫面代表本階段完成，可設定 `"complete": true`；若畫面需交回 AI 深判，可設定 `"handoff": true`。

AUTOGAMETEST_AUTOTUNE_SUMMARY:
```json
{{
  "changed": true,
  "files": ["SKILL.md 或 visual_memory 會由 runner 套用"],
  "summary": "建議 runner 套用什麼調整",
  "skipped_reason": ""
}}
```
"""


def run_autotune_job(job_id: str, engine: str = "codex",
                     timeout: int = 1800, model: str | None = None,
                     reasoning_effort: str | None = None,
                     print_prompt: bool = False) -> dict:
    job = store.get_job(job_id)
    if not job:
        return {"ok": False, "error": f"job 不存在: {job_id}"}
    payload = job.get("payload", {})
    source_job_id = payload.get("source_job_id", "")
    source_job = store.get_job(source_job_id)
    if not source_job:
        error = f"source job 不存在: {source_job_id}"
        store.update_job(job_id, status="error", result=error, progress=None)
        return {"ok": False, "error": error}
    game_id = payload.get("game_id") or source_job.get("payload", {}).get("game_id", "")
    prompt = build_autotune_prompt(job, source_job)
    if print_prompt:
        return {"ok": True, "prompt": prompt}
    store.update_job(job_id, status="running", progress="Codex 效能調整中")
    before = _git_status()
    started = time.perf_counter()
    result = ai_runner.run_with_fallback(
        prompt,
        cwd=ROOT,
        timeout=timeout,
        engine=engine,
        fallback=False,
        codex_sandbox="read-only",
        codex_model=model,
        codex_reasoning_effort=reasoning_effort,
    )
    visual_memory_merge = None
    visual_fast_rules_merge = None
    skill_lessons_update = None
    autotune_summary = extract_autotune_summary(result.get("output", ""))
    game_understanding = extract_game_understanding(result.get("output", ""))
    game_understanding_update = None
    if game_id and game_understanding:
        body = format_game_understanding(game_understanding)
        if body:
            game_understanding_update = store.upsert_skill_section(
                game_id,
                "遊戲理解",
                body,
                source=f"autotune:{source_job_id or job_id}",
            )
    learned_visuals = visual_memory.extract_memory_block(result.get("output", ""))
    if game_id and learned_visuals:
        visual_memory_merge = visual_memory.merge_entries(
            game_id, learned_visuals, source=f"autotune:{source_job_id or job_id}")
        visual_fast_rules_merge = (visual_memory_merge or {}).get("fast_rules")
    learned_lessons = extract_skill_lessons(result.get("output", ""))
    if game_id and learned_lessons:
        skill_lessons_update = store.append_skill_lessons(
            game_id,
            learned_lessons,
            source=f"autotune:{source_job_id or job_id}",
        )
    after = _git_status()
    elapsed = round(time.perf_counter() - started, 3)
    ok = bool(result.get("ok"))
    summary = (
        f"[engine={result.get('engine_used', 'codex')}] "
        f"效能調整{'完成' if ok else '失敗'}，{elapsed} 秒"
    )
    if visual_memory_merge:
        summary += (
            "\n\n圖片記憶評估："
            f"新增 {visual_memory_merge.get('added', 0)}、"
            f"更新 {visual_memory_merge.get('updated', 0)}，"
            f"共 {visual_memory_merge.get('total', 0)} 筆。"
        )
        if visual_fast_rules_merge:
            summary += (
                "\n由圖片記憶晉升 fast rules："
                f"新增 {visual_fast_rules_merge.get('added', 0)}、"
                f"更新 {visual_fast_rules_merge.get('updated', 0)}。"
            )
    if skill_lessons_update:
        summary += (
            "\n\nSkill 經驗教訓："
            f"追加 {skill_lessons_update.get('appended', 0)}、"
            f"略過 {skill_lessons_update.get('skipped', 0)}。"
        )
    if game_understanding_update:
        updated_text = "已更新" if game_understanding_update.get("updated") else "未更新"
        summary += f"\n\nSkill 遊戲理解：{updated_text}。"
    if visual_memory_merge or skill_lessons_update or game_understanding_update:
        summary += "\n落檔由 run_autotune.py 主流程完成，不依賴子 Codex 的寫檔權限。"
    if result.get("output"):
        summary += "\n\n" + result["output"][:2500]
    store.update_job(
        job_id,
        status="done" if ok else "error",
        engine_used=result.get("engine_used"),
        attempts=result.get("attempts", []),
        before_status=before,
        after_status=after,
        elapsed_seconds=elapsed,
        autotune_summary=autotune_summary,
        game_understanding=game_understanding_update,
        skill_lessons=skill_lessons_update,
        visual_memory=visual_memory_merge,
        fast_rules_from_visual_memory=visual_fast_rules_merge,
        progress=None,
        result=summary[:3000],
    )
    if source_job_id:
        store.update_job(
            source_job_id,
            autotune_job_id=job_id,
            autotune_status="done" if ok else "error",
            autotune_game_understanding=game_understanding_update,
            autotune_skill_lessons=skill_lessons_update,
            autotune_visual_memory=visual_memory_merge,
            autotune_fast_rules_from_visual_memory=visual_fast_rules_merge,
        )
    result.update({
        "elapsed_seconds": elapsed,
        "before_status": before,
        "after_status": after,
        "autotune_summary": autotune_summary,
        "game_understanding": game_understanding_update,
        "skill_lessons": skill_lessons_update,
        "visual_memory": visual_memory_merge,
        "fast_rules_from_visual_memory": visual_fast_rules_merge,
    })
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autotune agent performance with Codex")
    ap.add_argument("--job", required=True, help="autotune job id")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--model", default=None)
    ap.add_argument("--reasoning-effort", default=None,
                    choices=sorted(ai_runner.CODEX_REASONING_EFFORTS))
    ap.add_argument("--print-prompt", action="store_true")
    args = ap.parse_args(argv)
    result = run_autotune_job(
        args.job,
        engine=args.engine,
        timeout=args.timeout,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        print_prompt=args.print_prompt,
    )
    if args.print_prompt and result.get("ok"):
        print(result["prompt"])
        return 0
    if result.get("ok"):
        print(result.get("output", ""))
        return 0
    print(f"失敗：{result.get('error') or result.get('reason')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
