"""Run an AutoGameTest game agent, with automatic Claude -> Codex fallback.

This is the agent-level version of ai_runner: when running an agent through
Claude fails because the quota is exhausted, the same agent task is re-run
through Codex. It works by composing a fully self-contained prompt (persona +
skill knowledge + control cheatsheet + task) so that EITHER engine can execute
it without the interactive session's context.

Emulator (ADB) agents are the sweet spot: every control action is a shell
command (adb tap / screencap), which both Claude Code headless and Codex exec
can run. Desktop (computer-use) agents can't be driven headless the same way --
see the caveat printed for them.

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

from core import store, adb  # noqa: E402
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


def build_agent_prompt(game: dict, task: str) -> str:
    """Compose a self-contained agent prompt any engine can execute."""
    persona = _read(game.get("agent_path", ""))
    skill = _read(game.get("skill_path", ""))
    lc = game.get("launch", {})

    if game.get("control") == "emulator":
        serial = lc.get("serial") or adb.serial_for(lc.get("instance", 0))
        control = f"""# 控制方式（Android 模擬器 / ADB）
本機 Windows。adb 執行檔：`{adb.ADB}`，目標裝置：`{serial}`，遊戲 package：`{lc.get('package','')}`。
所有操作都用 shell 指令，不需要 GUI：
- 截圖並取回本機檢視：
  `"{adb.ADB}" -s {serial} shell screencap -p /sdcard/s.png`
  然後 `"{adb.ADB}" -s {serial} pull /sdcard/s.png <本機路徑>` 再讀取該圖。
- 點擊：`"{adb.ADB}" -s {serial} shell input tap <X> <Y>`
- 滑動：`"{adb.ADB}" -s {serial} shell input swipe <x1> <y1> <x2> <y2> <ms>`
- 啟動遊戲：`"{adb.ADB}" -s {serial} shell monkey -p {lc.get('package','')} -c android.intent.category.LAUNCHER 1`
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

# 本次任務
{task}

# 鐵則
- 每一步操作後截圖驗證畫面，不符預期就停下重判，不要盲目連點。
- 登入/帳密/付費畫面一律停止並回報，絕不代為輸入或消費。
- 只做低頻選單操作與單人模式，不自動打線上排位對戰。
- 完成後回報：做了哪些操作、獲得什麼（數值前後對照）、有無異常。
"""


def run_agent(agent_id=None, game_id=None, task=None, job_id=None,
              engine="auto", fallback=True, timeout=1200,
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

    prompt = build_agent_prompt(game, task)
    if print_only:
        return {"ok": True, "prompt": prompt}

    # emulator agents need adb (network + external exe) -> full access sandbox
    sandbox = "danger-full-access" if game.get("control") == "emulator" else "workspace-write"

    if job_id:
        store.update_job(job_id, status="running")

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

    if job_id:
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            engine_used=result.get("engine_used"),
            fallback_reason=result.get("reason", ""),
            attempts=_summarize_attempts(result.get("attempts", [])),
            result=_format_job_result(result),
            error_trace=(result.get("traceback") or "")[:4000] or None)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a game agent with Claude->Codex fallback")
    ap.add_argument("--agent", help="agent id")
    ap.add_argument("--game", help="game id (用 --task 搭配)")
    ap.add_argument("--task", help="任務內容（覆蓋 agent 預設 prompt）")
    ap.add_argument("--job", help="處理指定 job id 並回寫狀態")
    ap.add_argument("--engine", choices=["auto", "claude", "codex"], default="auto")
    ap.add_argument("--no-fallback", action="store_true")
    ap.add_argument("--timeout", type=int, default=1200)
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
                    engine=args.engine, fallback=not args.no_fallback,
                    timeout=args.timeout, print_only=args.print_prompt)

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
