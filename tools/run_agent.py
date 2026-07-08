"""Run an AutoGameTest game agent with Codex.

This composes a fully self-contained prompt (persona + skill knowledge +
control cheatsheet + task) and executes it through Codex CLI.

Emulator (ADB) agents are the sweet spot: every control action is a shell
command (adb tap / screencap), which Codex exec can run. Desktop
(computer-use) agents can't be driven headless the same way -- see the caveat
printed for them.

Usage:
    python tools/run_agent.py --agent masterduel-daily
    python tools/run_agent.py --game gget --task "完成每日任務"
    python tools/run_agent.py --job <job_id>        # process a queued job
    python tools/run_agent.py --agent <id> --print-prompt   # dry run, show prompt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # game reports are Chinese; avoid cp950 crash
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import store, adb, fast_agent, visual_memory  # noqa: E402
import ai_runner  # noqa: E402


def _summarize_attempts(attempts: list[dict]) -> list[dict]:
    rows = []
    for a in attempts:
        rows.append({
            "engine": a.get("engine"),
            "ok": a.get("ok"),
            "found": a.get("found"),
            "quota": a.get("quota"),
            "rc": a.get("rc"),
            "detail": (a.get("detail") or "")[:500],
        })
    return rows


def _format_job_result(result: dict) -> str:
    engine = result.get("engine_used", "unknown")
    reason = result.get("reason") or ""
    output = result.get("output") or ""
    head = f"[engine={engine}] {reason}".strip()
    if output:
        return (head + "\n\n" + output)[:3000]
    return head[:3000]


def _read(path_rel: str) -> str:
    path = os.path.join(ROOT, path_rel)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def build_agent_prompt(game: dict, task: str, fast_context: str = "",
                       visual_context: str = "") -> str:
    """Compose a self-contained agent prompt any engine can execute."""
    persona = _read(game.get("agent_path", ""))
    skill = _read(game.get("skill_path", ""))
    lc = game.get("launch", {})

    if game.get("control") == "emulator":
        emulator = adb.normalize_emulator(lc.get("emulator", "ldplayer"))
        serial = lc.get("serial") or adb.serial_for(lc.get("instance", 0), emulator)
        adb_path = adb.adb_path_for(emulator)
        control = f"""# 控制方式（Android 模擬器 / ADB）
本機 Windows。模擬器：`{emulator}`，adb 執行檔：`{adb_path}`，目標裝置：`{serial}`，遊戲 package：`{lc.get('package','')}`。
所有操作都用 shell 指令，不需要 GUI：
- 截圖並取回本機檢視：
  `"{adb_path}" -s {serial} shell screencap -p /sdcard/s.png`
  然後 `"{adb_path}" -s {serial} pull /sdcard/s.png <本機路徑>` 再讀取該圖。
- 點擊：`"{adb_path}" -s {serial} shell input tap <X> <Y>`
- 滑動：`"{adb_path}" -s {serial} shell input swipe <x1> <y1> <x2> <y2> <ms>`
- 啟動遊戲：`"{adb_path}" -s {serial} shell monkey -p {lc.get('package','')} -c android.intent.category.LAUNCHER 1`
每執行一個操作後務必重新截圖，確認畫面符合預期再進行下一步。"""
    else:
        cu = lc.get("cu_app_name", "")
        control = f"""# 控制方式（桌面遊戲）
此為桌面遊戲（平台 {game.get('platform')}），視窗標題「{lc.get('window_title','')}」，
computer-use 應用名稱「{cu}」。
注意：桌面遊戲需 computer-use（截圖+滑鼠鍵盤），headless/Codex 環境可能沒有這個工具。
若你（執行引擎）沒有 computer-use 能力，請不要盲目操作，改為回報「桌面 agent 需在具備 computer-use 的互動 session 執行」。"""

    return f"""你是一位遊戲玩家，代替使用者操作《{game.get('name','')}》完成指定任務。

# 角色與守則
{persona or '（無專屬 persona 檔，請以謹慎的遊戲玩家身分操作）'}

# 遊戲知識庫（Skill）
{skill or '（尚無 skill，請先謹慎探索並記錄）'}

{control}

{visual_context}

{fast_context}

# 本次任務
{task}

# 鐵則
- 每一步操作後截圖驗證畫面，不符預期就停下重判，不要盲目連點。
- 登入/帳密/付費畫面一律停止並回報，絕不代為輸入或消費。
- 只做低頻選單操作與單人模式，不自動打線上排位對戰。
- 完成後回報：做了哪些操作、獲得什麼（數值前後對照）、有無異常。
"""


def run_agent(agent_id=None, game_id=None, task=None, job_id=None,
              engine="codex", fallback=False, timeout=1200,
              fast_mode=True, fast_steps=8,
              print_only=False) -> dict:
    if agent_id:
        agent = store.get_agent(agent_id)
        if not agent:
            return {"ok": False, "error": f"agent 不存在: {agent_id}"}
        game_id = agent.get("game_id")
        task = task or agent.get("prompt", "")
    if not game_id:
        return {"ok": False, "error": "缺少 game_id / agent"}
    game = store.get_game(game_id)
    if not game:
        return {"ok": False, "error": f"遊戲不存在: {game_id}"}
    if not task:
        return {"ok": False, "error": "缺少任務內容"}

    if job_id:
        store.update_job(job_id, status="running")

    fast_result = None
    if fast_mode and game.get("control") == "emulator" and not print_only:
        try:
            fast_result = fast_agent.run_fast_rules(
                game, task=task, job_id=job_id, max_steps=fast_steps)
        except Exception as e:
            fast_result = {
                "enabled": True,
                "used": False,
                "completed": False,
                "handoff_reason": f"fast layer crashed: {e}",
                "error_trace": traceback.format_exc()[:4000],
            }
        if job_id:
            store.update_job(job_id, fast_decision=fast_result)
        if fast_result.get("completed"):
            output = (
                "快速規則已完成本次任務。\n\n"
                f"執行規則數：{len(fast_result.get('steps', []))}\n"
                f"交接原因：{fast_result.get('handoff_reason', '')}")
            result = {
                "engine_used": "fast-rules",
                "ok": True,
                "output": output,
                "reason": fast_result.get("handoff_reason", ""),
                "attempts": [],
                "fast_decision": fast_result,
            }
            if job_id:
                store.update_job(
                    job_id,
                    status="done",
                    engine_used="fast-rules",
                    run_reason=result["reason"],
                    attempts=[],
                    fast_decision=fast_result,
                    result=_format_job_result(result))
            return result

    fast_context = fast_agent.format_fast_context(game.get("id", ""), fast_result)
    visual_context = visual_memory.format_prompt_context(game.get("id", ""))
    prompt = build_agent_prompt(
        game, task, fast_context=fast_context, visual_context=visual_context)
    if print_only:
        return {"ok": True, "prompt": prompt}

    # emulator agents need adb (network + external exe) -> full access sandbox
    sandbox = "danger-full-access" if game.get("control") == "emulator" else "workspace-write"

    try:
        result = ai_runner.run_with_fallback(
            prompt, cwd=ROOT, timeout=timeout, engine=engine,
            fallback=fallback, codex_sandbox=sandbox)
    except Exception as e:
        result = {
            "engine_used": "none",
            "ok": False,
            "output": "",
            "reason": f"runner crashed: {e}",
            "attempts": [],
            "traceback": traceback.format_exc(),
        }

    learned_rules = fast_agent.extract_rule_block(result.get("output", ""))
    fast_rules_merge = None
    if learned_rules and game.get("control") == "emulator":
        fast_rules_merge = fast_agent.merge_rules(
            game.get("id", ""), learned_rules, source="codex-output")
    learned_visuals = visual_memory.extract_memory_block(result.get("output", ""))
    visual_memory_merge = None
    if learned_visuals:
        visual_memory_merge = visual_memory.merge_entries(
            game.get("id", ""), learned_visuals, source="codex-output")

    if job_id:
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            engine_used=result.get("engine_used"),
            run_reason=result.get("reason", ""),
            attempts=_summarize_attempts(result.get("attempts", [])),
            fast_decision=fast_result,
            fast_rules=fast_rules_merge,
            visual_memory=visual_memory_merge,
            result=_format_job_result(result),
            error_trace=(result.get("traceback") or "")[:4000] or None)
    if fast_rules_merge:
        result["fast_rules"] = fast_rules_merge
    if fast_result:
        result["fast_decision"] = fast_result
    if visual_memory_merge:
        result["visual_memory"] = visual_memory_merge
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a game agent with Codex")
    ap.add_argument("--agent", help="agent id")
    ap.add_argument("--game", help="game id (用 --task 搭配)")
    ap.add_argument("--task", help="任務內容（覆蓋 agent 預設 prompt）")
    ap.add_argument("--job", help="處理指定 job id 並回寫狀態")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--no-fast", action="store_true", help="停用 emulator 快速判斷層")
    ap.add_argument("--fast-steps", type=int, default=8, help="快速規則最多連續執行步數")
    ap.add_argument("--print-prompt", action="store_true", help="只組裝並印出 prompt，不執行")
    args = ap.parse_args(argv)

    agent_id, game_id, task = args.agent, args.game, args.task
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr); return 2
        p = job.get("payload", {})
        agent_id = agent_id or p.get("agent_id")
        game_id = game_id or p.get("game_id")
        task = task or p.get("prompt") or p.get("task")

    res = run_agent(agent_id=agent_id, game_id=game_id, task=task, job_id=args.job,
                    engine=args.engine, fallback=False,
                    timeout=args.timeout, fast_mode=not args.no_fast,
                    fast_steps=args.fast_steps, print_only=args.print_prompt)

    if args.print_prompt and res.get("ok"):
        print(res["prompt"]); return 0
    if not res.get("ok") and "error" in res:
        print(f"錯誤：{res['error']}", file=sys.stderr); return 2

    print(res.get("output", ""))
    print(f"\n[引擎: {res.get('engine_used')}] ok={res.get('ok')} "
          f"{res.get('reason','')}", file=sys.stderr)
    if res.get("attempts"):
        print(json.dumps(_summarize_attempts(res["attempts"]),
                         ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
