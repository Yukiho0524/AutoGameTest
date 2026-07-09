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
import re
import sys
import time
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
            "model": a.get("model"),
            "reasoning_effort": a.get("reasoning_effort"),
            "elapsed_seconds": a.get("elapsed_seconds"),
            "prompt_chars": a.get("prompt_chars"),
            "segment_index": a.get("segment_index"),
            "quota": a.get("quota"),
            "rc": a.get("rc"),
            "detail": (a.get("detail") or "")[:500],
        })
    return rows


def _resolve_codex_settings(model: str | None = None,
                            reasoning_effort: str | None = None) -> tuple[str, str]:
    settings = store.get_settings()
    model = str(model or settings.get("codex_model") or "gpt-5.5").strip() or "gpt-5.5"
    reasoning_effort = (
        str(reasoning_effort or settings.get("codex_reasoning_effort") or "high")
        .strip()
        .lower()
    )
    if reasoning_effort not in ai_runner.CODEX_REASONING_EFFORTS:
        reasoning_effort = "high"
    return model, reasoning_effort


def split_numbered_task(task: str) -> list[str]:
    """Split 1./2./3. style task text into checkpoints."""
    lines = [line.strip() for line in str(task or "").splitlines() if line.strip()]
    if not lines:
        return []
    parts: list[str] = []
    current: list[str] = []
    marker = re.compile(r"^\s*(?:\d+|[一二三四五六七八九十]+)[\.\)、:：]\s*(.+)$")
    saw_marker = False
    for line in lines:
        match = marker.match(line)
        if match:
            saw_marker = True
            if current:
                parts.append(" ".join(current).strip())
            current = [match.group(1).strip()]
        elif current:
            current.append(line)
        else:
            current = [line]
    if current:
        parts.append(" ".join(current).strip())
    if not saw_marker or len(parts) < 2:
        text = str(task or "").strip()
        return [text] if text else []
    return [part for part in parts if part]


def _count_artifact_pngs(fast_result: dict | None) -> int:
    artifact_dir = (fast_result or {}).get("artifact_dir")
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return 0
    return len([
        name for name in os.listdir(artifact_dir)
        if name.lower().endswith(".png")
    ])


def _base_metrics(game: dict, task: str, model: str,
                  reasoning_effort: str) -> dict:
    persona = _read(game.get("agent_path", ""))
    skill = _read(game.get("skill_path", ""))
    return {
        "mode": "single",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "task_chars": len(task or ""),
        "persona_chars": len(persona),
        "skill_chars": len(skill),
        "visual_context_chars": 0,
        "fast_context_chars": 0,
        "prompt_chars": 0,
        "segments_total": 1,
        "segments_completed": 0,
        "segment_timeout_seconds": None,
        "fast_seconds": 0,
        "codex_seconds": 0,
        "total_seconds": 0,
        "artifact_png_count": 0,
        "segments": [],
    }


def _segment_task(overall_task: str, part: str, index: int, total: int,
                  previous_summaries: list[str]) -> str:
    previous = "\n".join(
        f"- {summary[:500]}" for summary in previous_summaries[-3:]
        if summary.strip())
    if not previous:
        previous = "- 尚無"
    return f"""這是一個分段任務，請只完成目前這一段，完成後就停止，不要提前執行後面的段落。

整體任務：
{overall_task}

已完成段落摘要：
{previous}

目前段落（{index}/{total}）：
{part}

執行要求：
- 本段開始時先重新截圖確認目前畫面，不要依賴上一段截圖。
- 只做目前段落需要的低風險操作；遇到登入、付款、轉蛋、PVP 或不確定畫面就停止回報。
- 本段完成後輸出一段 `CHECKPOINT_SUMMARY:`，簡短說明目前畫面、已完成什麼、下一段可以從哪裡接續。
"""


def _checkpoint_summary(output: str) -> str:
    text = output or ""
    marker = "CHECKPOINT_SUMMARY:"
    idx = text.rfind(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    lines = [line.strip(" -") for line in text.splitlines() if line.strip()]
    return " ".join(lines[:4])[:800]


def _merge_segment_attempts(segment_result: dict, index: int) -> list[dict]:
    attempts = []
    for attempt in segment_result.get("attempts", []):
        item = dict(attempt)
        item["segment_index"] = index
        attempts.append(item)
    return attempts


def _format_job_result(result: dict) -> str:
    engine = result.get("engine_used", "unknown")
    reason = result.get("reason") or ""
    output = result.get("output") or ""
    head = f"[engine={engine}] {reason}".strip()
    if output:
        return (head + "\n\n" + output)[:3000]
    return head[:3000]


def _extract_marked_json(text: str, marker: str):
    idx = text.find(marker)
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
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(tail.strip())
        return obj
    except json.JSONDecodeError:
        return tail.strip()


def extract_skill_lessons(text: str) -> list[str]:
    obj = _extract_marked_json(text or "", "AUTOGAMETEST_SKILL_LESSONS")
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
- 若本次學到可重用的 UI 修正、流程變化、錯誤原因或安全操作經驗，請在最終回覆附上：
AUTOGAMETEST_SKILL_LESSONS:
```json
["可重用教訓 1", "可重用教訓 2"]
```
  只放精煉教訓，不要放逐步流水帳、帳密、token、購買資訊或一次性雜訊。
"""


def run_agent(agent_id=None, game_id=None, task=None, job_id=None,
              engine="codex", fallback=False, timeout=3600,
              fast_mode=True, fast_steps=8,
              codex_model: str | None = None,
              codex_reasoning_effort: str | None = None,
              segment_mode: bool = True,
              print_only=False) -> dict:
    total_start = time.perf_counter()
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

    codex_model, codex_reasoning_effort = _resolve_codex_settings(
        codex_model, codex_reasoning_effort)
    performance = _base_metrics(
        game, task, codex_model, codex_reasoning_effort)

    if job_id:
        store.update_job(
            job_id,
            status="running",
            codex_model=codex_model,
            codex_reasoning_effort=codex_reasoning_effort,
            performance=performance,
        )

    fast_result = None
    fast_start = time.perf_counter()
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
    performance["fast_seconds"] = round(time.perf_counter() - fast_start, 3)
    performance["artifact_png_count"] = _count_artifact_pngs(fast_result)
    if fast_result:
        performance["fast_steps"] = len(fast_result.get("steps", []))
        performance["fast_handoff_reason"] = fast_result.get("handoff_reason", "")
        performance["fast_rules_loaded"] = fast_result.get("fast_rules_loaded", 0)
        performance["visual_rules_loaded"] = fast_result.get("visual_rules_loaded", 0)
    if fast_result and not print_only:
        if job_id:
            store.update_job(job_id, fast_decision=fast_result,
                             performance=performance)
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
            performance["mode"] = "fast-rules"
            performance["segments_completed"] = 1
            performance["total_seconds"] = round(time.perf_counter() - total_start, 3)
            if job_id:
                store.update_job(
                    job_id,
                    status="done",
                    engine_used="fast-rules",
                    run_reason=result["reason"],
                    attempts=[],
                    fast_decision=fast_result,
                    performance=performance,
                    result=_format_job_result(result))
            return result

    fast_context = fast_agent.format_fast_context(game.get("id", ""), fast_result)
    visual_context = visual_memory.format_prompt_context(game.get("id", ""))
    performance["fast_context_chars"] = len(fast_context)
    performance["visual_context_chars"] = len(visual_context)
    task_parts = split_numbered_task(task)
    use_segments = segment_mode and len(task_parts) >= 2
    performance["mode"] = "segmented" if use_segments else "single"
    performance["segments_total"] = len(task_parts) if use_segments else 1
    segment_timeout = max(300, int(timeout / max(1, len(task_parts)))) if use_segments else timeout
    performance["segment_timeout_seconds"] = segment_timeout if use_segments else None
    prompt = build_agent_prompt(
        game, task, fast_context=fast_context, visual_context=visual_context)
    performance["prompt_chars"] = len(prompt)
    if print_only:
        return {"ok": True, "prompt": prompt, "performance": performance}

    # emulator agents need adb (network + external exe) -> full access sandbox
    sandbox = "danger-full-access" if game.get("control") == "emulator" else "workspace-write"

    try:
        if use_segments:
            outputs: list[str] = []
            attempts: list[dict] = []
            summaries: list[str] = []
            failed_reason = ""
            for index, part in enumerate(task_parts, 1):
                segment_task = _segment_task(task, part, index, len(task_parts), summaries)
                segment_prompt = build_agent_prompt(
                    game, segment_task,
                    fast_context=fast_context,
                    visual_context=visual_context)
                segment_info = {
                    "index": index,
                    "task": part[:500],
                    "prompt_chars": len(segment_prompt),
                    "timeout_seconds": segment_timeout,
                    "status": "running",
                }
                performance["segments"].append(segment_info)
                if job_id:
                    store.update_job(
                        job_id,
                        progress={
                            "current_segment": index,
                            "total_segments": len(task_parts),
                            "task": part,
                        },
                        performance=performance,
                    )
                segment_result = ai_runner.run_with_fallback(
                    segment_prompt, cwd=ROOT, timeout=segment_timeout,
                    engine=engine, fallback=fallback,
                    codex_sandbox=sandbox,
                    codex_model=codex_model,
                    codex_reasoning_effort=codex_reasoning_effort)
                segment_attempts = _merge_segment_attempts(segment_result, index)
                attempts.extend(segment_attempts)
                elapsed = sum(float(a.get("elapsed_seconds") or 0) for a in segment_attempts)
                output = segment_result.get("output", "")
                summary = _checkpoint_summary(output)
                if summary:
                    summaries.append(summary)
                segment_info.update({
                    "status": "done" if segment_result.get("ok") else "error",
                    "ok": bool(segment_result.get("ok")),
                    "elapsed_seconds": round(elapsed, 3),
                    "output_chars": len(output),
                    "reason": segment_result.get("reason", ""),
                    "summary": summary,
                })
                performance["segments_completed"] = index if segment_result.get("ok") else index - 1
                performance["codex_seconds"] = round(
                    sum(float(a.get("elapsed_seconds") or 0) for a in attempts), 3)
                if job_id:
                    store.update_job(job_id, performance=performance)
                outputs.append(
                    f"## Segment {index}/{len(task_parts)}: {part}\n\n{output}".strip())
                if not segment_result.get("ok"):
                    failed_reason = (
                        f"segment {index}/{len(task_parts)} failed: "
                        f"{segment_result.get('reason', '')}")
                    break
            ok = not failed_reason
            result = {
                "engine_used": "codex-segmented",
                "ok": ok,
                "output": "\n\n".join(outputs),
                "attempts": attempts,
                "reason": (
                    f"completed {len(task_parts)} segments"
                    if ok else failed_reason),
            }
        else:
            result = ai_runner.run_with_fallback(
                prompt, cwd=ROOT, timeout=timeout, engine=engine,
                fallback=fallback, codex_sandbox=sandbox,
                codex_model=codex_model,
                codex_reasoning_effort=codex_reasoning_effort)
            performance["segments_completed"] = 1 if result.get("ok") else 0
            performance["codex_seconds"] = round(sum(
                float(a.get("elapsed_seconds") or 0)
                for a in result.get("attempts", [])
            ), 3)
    except Exception as e:
        result = {
            "engine_used": "none",
            "ok": False,
            "output": "",
            "reason": f"runner crashed: {e}",
            "attempts": [],
            "traceback": traceback.format_exc(),
        }
    performance["total_seconds"] = round(time.perf_counter() - total_start, 3)
    performance["artifact_png_count"] = _count_artifact_pngs(fast_result)
    result["performance"] = performance

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
    learned_lessons = extract_skill_lessons(result.get("output", ""))
    skill_lessons_update = None
    if learned_lessons:
        skill_lessons_update = store.append_skill_lessons(
            game.get("id", ""),
            learned_lessons,
            source=f"run_agent:{job_id or 'manual'}",
        )

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
            skill_lessons=skill_lessons_update,
            performance=performance,
            progress=None,
            result=_format_job_result(result),
            error_trace=(result.get("traceback") or "")[:4000] or None)
    if fast_rules_merge:
        result["fast_rules"] = fast_rules_merge
    if fast_result:
        result["fast_decision"] = fast_result
    if visual_memory_merge:
        result["visual_memory"] = visual_memory_merge
    if skill_lessons_update:
        result["skill_lessons"] = skill_lessons_update
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a game agent with Codex")
    ap.add_argument("--agent", help="agent id")
    ap.add_argument("--game", help="game id (用 --task 搭配)")
    ap.add_argument("--task", help="任務內容（覆蓋 agent 預設 prompt）")
    ap.add_argument("--job", help="處理指定 job id 並回寫狀態")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--model", default=None, help="Codex model，預設 gpt-5.5")
    ap.add_argument("--reasoning-effort", default=None,
                    choices=sorted(ai_runner.CODEX_REASONING_EFFORTS),
                    help="Codex reasoning effort，預設 high")
    ap.add_argument("--no-fast", action="store_true", help="停用 emulator 快速判斷層")
    ap.add_argument("--no-segment", action="store_true", help="停用條列任務自動分段")
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
                    fast_steps=args.fast_steps,
                    codex_model=args.model,
                    codex_reasoning_effort=args.reasoning_effort,
                    segment_mode=not args.no_segment,
                    print_only=args.print_prompt)

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
