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

# 可調整檔案
{json.dumps(allowed_paths, ensure_ascii=False, indent=2)}

# 建議調整方向
- 如果效能建議提到 fast layer 沒命中：在 Skill/Agent 中加入「遇到已知安全畫面要輸出 AUTOGAMETEST_FAST_RULES 或 VISUAL_MEMORY」的精煉教訓；不要憑空造座標。
- 如果 Codex 判斷耗時很長：把可重複的完成判定、已知畫面狀態、停止條件寫進 Skill，讓下次少推理。
- 如果分段任務後段重複確認相同畫面：寫入「畫面已符合前序步驟時直接承認完成，不退回重做」的遊戲專屬規則。
- 如果 prompt/skill 太長：只整理該遊戲 Skill 中重複的經驗教訓，保留安全邊界。
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
    after = _git_status()
    elapsed = round(time.perf_counter() - started, 3)
    ok = bool(result.get("ok"))
    summary = (
        f"[engine={result.get('engine_used', 'codex')}] "
        f"效能調整{'完成' if ok else '失敗'}，{elapsed} 秒"
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
        progress=None,
        result=summary[:3000],
    )
    if source_job_id:
        store.update_job(
            source_job_id,
            autotune_job_id=job_id,
            autotune_status="done" if ok else "error",
        )
    result.update({
        "elapsed_seconds": elapsed,
        "before_status": before,
        "after_status": after,
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
