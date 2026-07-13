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


def collect_visual_candidates(source_job: dict, limit: int = 18) -> list[dict]:
    """Collect existing artifact screenshots worth asking autotune to review."""
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(path: str, source: str, detail: dict | None = None) -> None:
        if len(candidates) >= limit:
            return
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
            "reason": str(turn.get("reason") or "")[:300],
            "observation": str(turn.get("observation") or "")[:500],
            "learned": str(turn.get("learned") or "")[:500],
            "next_state": str(turn.get("next_state") or "")[:300],
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
                if len(candidates) >= limit:
                    break
                if os.path.splitext(name)[1].lower() not in visual_memory.IMAGE_EXTS:
                    continue
                add(os.path.join(full_dir, name), "artifact_dir", {
                    "artifact_dir": _rel(full_dir),
                })
    return candidates


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
    current_visual_memory = visual_memory.summary(game_id, limit=12)
    skill = _read_rel(skill_path)
    agent_text = _read_rel(agent_path)
    current_status = _git_status()
    return f"""你是 AutoGameTest 的「Agent 效能調整器」。請根據一次已完成的 Agent 執行結果與效能診斷，對專案做**最小、保守、可回滾**的調整，目標是讓同一遊戲下次判斷更快、更少重複分析。

# 絕對邊界
- 不要操作遊戲、不跑 adb、不截圖、不登入、不購買、不抽卡、不進 PVP。
- 不要修改通用應用程式碼，除非效能診斷明確指出是程式 bug；本任務預設只調整知識檔。
- 不要 git commit / git push。
- 不要覆蓋使用者未提交修改；若檔案已有內容，只追加或小幅整理相關段落。
- 不要根據猜測創建 fast rule。只有在 source job 已明確包含安全 `fast_rules` 或截圖 signature + 安全動作時，才可補齊安全本地規則。
- 登入、付費、購買、抽卡/轉蛋、PVP/排位相關畫面只能寫成風險教訓，禁止建立可自動點擊規則。
- 可以評估 source job 已存在的 artifact 截圖是否值得加入圖片記憶；不要自行截圖或操作遊戲。

# 可調整檔案
{json.dumps(allowed_paths, ensure_ascii=False, indent=2)}

# 建議調整方向
- 如果效能建議提到 fast layer 沒命中：在 Skill/Agent 中加入「遇到已知安全畫面要輸出 AUTOGAMETEST_FAST_RULES 或 VISUAL_MEMORY」的精煉教訓；不要憑空造座標。
- 如果 Codex 判斷耗時很長：把可重複的完成判定、已知畫面狀態、停止條件寫進 Skill，讓下次少推理。
- 如果分段任務後段重複確認相同畫面：寫入「畫面已符合前序步驟時直接承認完成，不退回重做」的遊戲專屬規則。
- 如果 prompt/skill 太長：只整理該遊戲 Skill 中重複的經驗教訓，保留安全邊界。
- 檢查「圖片記憶候選截圖」：穩定、可辨識、未重複且未來會再次出現的畫面，請輸出 `AUTOGAMETEST_VISUAL_MEMORY`。
- 對登入、付費、抽卡、PVP、未知高風險畫面，只能建立 `risk: "high"` / `"manual"` / `"pvp"` 等風險記憶，不要給 actions、fast_match 或 complete。
- 對主畫面、一般選單、完成畫面、loading、已知安全彈窗，可建立 safe/low/routine 記憶；只有安全且可重複的畫面才附 actions。
- 若沒有足夠資訊安全調整，請不要改檔，只在輸出說明原因。

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

# fast_decision 摘要
```json
{_clip(fast_decision, 7000)}
```

# 圖片記憶候選截圖
以下都是 source job 已保存的截圖路徑；可用這些 `image_path` 建立 visual memory。若候選沒有可重用價值，請不要硬加。
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

請直接做必要的最小調整。最後輸出：
如果評估出可加入圖片記憶的截圖，請額外輸出：
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
    "regions": [{{"name": "任務入口", "x": 1000, "y": 620, "w": 120, "h": 80, "note": "安全入口"}}],
    "actions": [{{"type": "tap", "x": 1000, "y": 620, "wait": 0.8, "note": "打開任務"}}]
  }}
]
```

AUTOGAMETEST_AUTOTUNE_SUMMARY:
```json
{{
  "changed": true,
  "files": ["..."],
  "summary": "做了什麼調整",
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
        codex_sandbox="workspace-write",
        codex_model=model,
        codex_reasoning_effort=reasoning_effort,
    )
    visual_memory_merge = None
    visual_fast_rules_merge = None
    learned_visuals = visual_memory.extract_memory_block(result.get("output", ""))
    if game_id and learned_visuals:
        visual_memory_merge = visual_memory.merge_entries(
            game_id, learned_visuals, source=f"autotune:{source_job_id or job_id}")
        visual_fast_rules_merge = (visual_memory_merge or {}).get("fast_rules")
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
            autotune_visual_memory=visual_memory_merge,
            autotune_fast_rules_from_visual_memory=visual_fast_rules_merge,
        )
    result.update({
        "elapsed_seconds": elapsed,
        "before_status": before,
        "after_status": after,
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
