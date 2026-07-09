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
import subprocess
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

DEFAULT_SEGMENT_TIMEOUT_SECONDS = 600
AUTO_SEGMENT_MIN_STEPS = 5
DEFAULT_SEGMENT_BATCH_SIZE = 2
COMMON_MOBILE_CONTROLS_SKILL = ".codex/skills/mobile-game-controls/SKILL.md"
_CREATE_NO_WINDOW = 0x08000000
LOG_DIR = os.path.join(ROOT, "data", "logs")


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


def _spawn_autotune_runner(job_id: str, timeout: int,
                           model: str, reasoning_effort: str) -> bool:
    runner = os.path.join(ROOT, "tools", "run_autotune.py")
    if not os.path.isfile(runner):
        return False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        out_path = os.path.join(LOG_DIR, f"{job_id}.out.log")
        err_path = os.path.join(LOG_DIR, f"{job_id}.err.log")
        out = open(out_path, "w", encoding="utf-8")
        err = open(err_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, runner,
                    "--job", job_id,
                    "--engine", "codex",
                    "--timeout", str(timeout),
                    "--model", model,
                    "--reasoning-effort", reasoning_effort,
                ],
                cwd=ROOT,
                creationflags=_CREATE_NO_WINDOW,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
            )
        finally:
            out.close()
            err.close()
        store.update_job(
            job_id,
            log_stdout=out_path,
            log_stderr=err_path,
            runner_pid=proc.pid,
            runner_started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            codex_model=model,
            codex_reasoning_effort=reasoning_effort,
            ai_timeout_seconds=timeout,
        )
        return True
    except Exception:
        return False


def _enqueue_autotune_job(source_job_id: str | None, game: dict,
                          agent_id: str | None,
                          performance_analysis: dict | None,
                          result: dict | None,
                          model: str,
                          reasoning_effort: str) -> dict | None:
    if not source_job_id or not performance_analysis:
        return None
    settings = store.get_settings()
    if settings.get("auto_tune_after_agent") is False:
        return None
    recommendations = performance_analysis.get("recommendations") or []
    observations = performance_analysis.get("observations") or []
    if not recommendations and not observations:
        return None
    timeout = min(1800, int(settings.get("ai_timeout_seconds", 3600) or 3600))
    timeout = max(300, timeout)
    payload = {
        "source_job_id": source_job_id,
        "game_id": game.get("id", ""),
        "agent_id": agent_id or "",
        "performance_status": performance_analysis.get("status", ""),
        "recommendations": recommendations[:8],
        "observations": observations[:8],
        "source_engine": (result or {}).get("engine_used", ""),
        "source_ok": bool((result or {}).get("ok")),
    }
    job = store.enqueue_job("autotune_agent", payload)
    spawned = _spawn_autotune_runner(
        job["id"], timeout=timeout, model=model, reasoning_effort=reasoning_effort)
    job["spawned"] = spawned
    store.update_job(
        source_job_id,
        autotune_job_id=job["id"],
        autotune_status="running" if spawned else "error",
    )
    if not spawned:
        store.update_job(
            job["id"],
            status="error",
            result="無法啟動 tools/run_autotune.py 背景執行器",
        )
    return job


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


def batch_task_parts(parts: list[str], batch_size: int) -> list[dict]:
    """Group numbered steps into small checkpoint batches."""
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = DEFAULT_SEGMENT_BATCH_SIZE
    batch_size = max(1, min(4, batch_size))
    batches: list[dict] = []
    for start in range(0, len(parts), batch_size):
        chunk = parts[start:start + batch_size]
        numbered = "\n".join(
            f"{step_no}. {text}" for step_no, text in
            enumerate(chunk, start=start + 1)
        )
        batches.append({
            "start_step": start + 1,
            "end_step": start + len(chunk),
            "steps": chunk,
            "task": numbered,
        })
    return batches


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


def _float_seconds(value) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _stage_label(stage: str) -> str:
    labels = {
        "adb_backend_check": "ADB 後端檢查",
        "adb_ready_check": "模擬器 Ready 檢查",
        "launch_emulator": "啟動模擬器",
        "wait_after_emulator_launch": "等待模擬器啟動",
        "foreground_app_check": "前景 App 檢查",
        "launch_app": "啟動遊戲 App",
        "wait_after_app_launch": "等待遊戲啟動",
        "first_screenshot": "首張截圖",
        "screenshot": "截圖",
        "fast_rule_match": "快速規則比對",
        "post_action_screenshot": "操作後截圖",
        "codex_decision": "Codex 判斷與操作",
    }
    if stage.startswith("fast_action_"):
        return "快速操作"
    return labels.get(stage, stage)


def analyze_performance(performance: dict, fast_result: dict | None = None,
                        result: dict | None = None,
                        fast_rules_merge: dict | None = None,
                        visual_memory_merge: dict | None = None) -> dict:
    total = _float_seconds(performance.get("total_seconds"))
    stages: list[dict] = []
    for item in (fast_result or {}).get("timings", []):
        if not isinstance(item, dict):
            continue
        seconds = _float_seconds(item.get("seconds"))
        if seconds <= 0:
            continue
        stage = str(item.get("stage") or "unknown")
        stages.append({
            "stage": stage,
            "label": _stage_label(stage),
            "seconds": round(seconds, 3),
            "detail": str(item.get("detail") or ""),
            "ok": item.get("ok"),
        })
    segment_stages: list[dict] = []
    for item in performance.get("segments", []):
        if not isinstance(item, dict):
            continue
        seconds = _float_seconds(item.get("elapsed_seconds"))
        if seconds <= 0:
            continue
        index = item.get("index") or len(segment_stages) + 1
        start_step = item.get("start_step")
        end_step = item.get("end_step")
        step_range = (
            f"steps {start_step}-{end_step}"
            if start_step and end_step else "steps unknown")
        detail = item.get("summary") or item.get("task") or ""
        row = {
            "stage": f"codex_segment_{index}",
            "label": f"Codex 分段 {index}",
            "seconds": round(seconds, 3),
            "detail": f"{step_range}: {str(detail)[:140]}",
            "ok": item.get("ok"),
        }
        segment_stages.append(row)
        stages.append(row)
    codex_seconds = _float_seconds(performance.get("codex_seconds"))
    if codex_seconds > 0:
        stages.append({
            "stage": "codex_decision",
            "label": _stage_label("codex_decision"),
            "seconds": round(codex_seconds, 3),
            "detail": f"{performance.get('model')} + {performance.get('reasoning_effort')}",
            "ok": bool((result or {}).get("ok")),
        })
    if not stages and performance.get("fast_seconds"):
        stages.append({
            "stage": "fast_layer_total",
            "label": "Fast layer 總耗時",
            "seconds": round(_float_seconds(performance.get("fast_seconds")), 3),
            "detail": performance.get("fast_handoff_reason", ""),
            "ok": None,
        })
    stages.sort(key=lambda row: row["seconds"], reverse=True)
    bottleneck = stages[0] if stages else None
    observations: list[str] = []
    recommendations: list[str] = []

    def stage_seconds(name: str) -> float:
        return sum(row["seconds"] for row in stages if row["stage"] == name)

    emulator_wait = stage_seconds("wait_after_emulator_launch") + stage_seconds("launch_emulator")
    app_launch = stage_seconds("launch_app") + stage_seconds("wait_after_app_launch")
    first_screenshot = stage_seconds("first_screenshot")
    prompt_chars = int(performance.get("prompt_chars") or 0)
    handoff = str(performance.get("fast_handoff_reason") or "")

    if bottleneck:
        share = (bottleneck["seconds"] / total * 100) if total > 0 else 0
        observations.append(
            f"最慢階段是「{bottleneck['label']}」，耗時 {bottleneck['seconds']} 秒"
            + (f"，約佔總時間 {share:.0f}%" if share else "")
            + "。"
        )
    if emulator_wait >= 5:
        observations.append(f"模擬器啟動/暖機耗時 {emulator_wait:.1f} 秒。")
        recommendations.append("排程或手動執行前先保持模擬器常駐；之後可做排程前預熱，避開冷啟動。")
    if app_launch >= 2.5:
        observations.append(f"遊戲 App 啟動等待耗時 {app_launch:.1f} 秒。")
        recommendations.append("若遊戲已在前景，runner 會跳過重新 launch；若仍偏慢，優先檢查模擬器效能與遊戲載入畫面。")
    if first_screenshot >= 3:
        observations.append(f"首張截圖耗時 {first_screenshot:.1f} 秒。")
        recommendations.append("ADB 截圖偏慢時，優先確認模擬器解析度、ADB 後端與電腦負載。")
    if handoff == "no matching fast rule":
        observations.append("fast layer 已拿到畫面，但沒有命中安全快速規則。")
        recommendations.append("把穩定安全畫面加入 visual memory 或 fast rules，下次同畫面可少走 Codex 判斷。")
    if codex_seconds >= 60:
        observations.append(f"Codex 判斷耗時 {codex_seconds:.1f} 秒。")
        recommendations.append("若同一畫面常重複出現，應沉澱成圖片記憶或快速規則，讓本地層先處理。")
    if prompt_chars >= 12000:
        observations.append(f"Prompt 長度 {prompt_chars} 字元，可能拖慢模型判斷。")
        recommendations.append("整理 Skill，保留穩定流程與畫面規則，移除一次性流水帳。")
    if performance.get("mode") == "segmented":
        if segment_stages:
            slowest_segment = max(segment_stages, key=lambda row: row["seconds"])
            observations.append(
                f"已分成 {len(segment_stages)} 段；最慢分段是"
                f"「{slowest_segment['label']}」，耗時 {slowest_segment['seconds']} 秒。")
        recommendations.append("觀察各分段耗時；若某段仍偏慢，將該段畫面沉澱成 fast rules 或圖片記憶。")
    if fast_rules_merge and (fast_rules_merge.get("added") or fast_rules_merge.get("updated")):
        recommendations.append("本次已合併新的快速規則，下一次遇到同類畫面應會更快。")
    visual_fast_rules = (visual_memory_merge or {}).get("fast_rules") or {}
    if visual_fast_rules.get("added") or visual_fast_rules.get("updated"):
        recommendations.append(
            "本次圖片記憶已自動晉升為快速規則，下一次遇到同畫面會先走本地判斷。")
    if visual_memory_merge and (visual_memory_merge.get("added") or visual_memory_merge.get("updated")):
        recommendations.append("本次已合併新的圖片記憶，後續畫面辨識會更穩。")
    if not observations:
        observations.append("目前沒有明顯單一慢點。")
    if not recommendations:
        recommendations.append("持續累積圖片記憶與 fast rules，讓重複流程逐步本地化。")

    status = "ok"
    if codex_seconds >= 120 or emulator_wait >= 10 or first_screenshot >= 6:
        status = "slow"
    elif codex_seconds >= 60 or app_launch >= 4 or handoff == "no matching fast rule":
        status = "watch"

    return {
        "status": status,
        "total_seconds": round(total, 3),
        "bottleneck": bottleneck,
        "observations": observations[:8],
        "recommendations": recommendations[:8],
        "stages": stages[:12],
    }


def _segment_task(overall_task: str, part: str, index: int, total: int,
                  previous_summaries: list[str]) -> str:
    previous = "\n".join(
        f"- {summary[:500]}" for summary in previous_summaries[-3:]
        if summary.strip())
    if not previous:
        previous = "- 尚無"
    return f"""這是一個短 checkpoint 分段任務。只完成目前這一段，完成後立刻停止並回報，不要提前執行後面的段落。

完整任務只供理解流程，不代表現在要全部執行：
{overall_task}

已完成段落摘要：
{previous}

目前段落（{index}/{total}）：
{part}

執行要求：
- 本段開始時先重新截圖確認目前畫面，不要依賴上一段截圖。
- 只做目前段落需要的低風險操作；不要操作下一段。
- 本段通常包含 1 到 2 個使用者步驟；以最短路徑完成，通常 2 到 6 次 tap/swipe 就應該停止。
- 如果進入本段時畫面已經符合部分或全部目標，直接把已完成的部分記入摘要，不要退回重做。
- 一旦畫面符合本段目標，立刻輸出 `CHECKPOINT_SUMMARY:` 並結束，不要繼續探索或建立額外規則。
- 遇到登入、付款、轉蛋、PVP 或不確定畫面就停止回報。
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
        if engine == "codex-segmented" and len(output) > 2800:
            return (head + "\n\n...[前段分段輸出已省略]\n"
                    + output[-2600:])[:3000]
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
    common_skill = ""
    lc = game.get("launch", {})

    if game.get("control") == "emulator":
        common_skill = _read(COMMON_MOBILE_CONTROLS_SKILL)
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

    common_skill_block = (
        f"# 共用手機操作詞彙（Skill）\n{common_skill}"
        if common_skill else ""
    )

    return f"""你是一位遊戲玩家，代替使用者操作《{game.get('name','')}》完成指定任務。

# 角色與守則
{persona or '（無專屬 persona 檔，請以謹慎的遊戲玩家身分操作）'}

# 遊戲知識庫（Skill）
{skill or '（尚無 skill，請先謹慎探索並記錄）'}

{common_skill_block}

{control}

{visual_context}

{fast_context}

# 本次任務
{task}

# 完成判定與收尾
- 任務最後一句若包含「結束任務」「完成後通知我」「到最後通知我」「結束」等語意，代表那是完成條件，不是新的操作目標。
- 當最後一個指定目標已完成，或畫面/結果已能證明任務完成，立刻輸出最終回報並結束，不要停在完成畫面等待額外指令。
- 若只剩確認是否完成，最多再截圖驗證一次；確認後直接 done，不要重複點擊或重新探索。
- 最終回報要明確寫出「完成」或「未完成」，並列出已做操作、獲得/消耗、異常與停止原因。

# 速度策略
- 優先用目前畫面、Skill、圖片記憶與 fast layer 交接資訊判斷下一步，不要為同一畫面反覆長篇分析。
- 若任務是條列步驟，而且目前畫面已經在第 2 步或更後面的狀態，將前面已達成的步驟視為完成，直接接續下一個未完成步驟，不要退回重做。
- 若已能確認任務完成，立即停止並回報，不要繼續探索或多做不必要操作。
- 每輪最多做 1 到 3 個低風險操作；每個操作後截圖驗證，畫面不符預期就停下重判。
- 載入中或轉場中可短暫等待後重截圖；不要因一次暫時性截圖/載入失敗就展開大範圍探索。
- 遇到未知高風險、登入、付費、轉蛋、PVP 或排位畫面，停止並回報。

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
              segment_mode: bool = False,
              auto_segment: bool = False,
              segment_timeout: int | None = None,
              segment_batch_size: int = DEFAULT_SEGMENT_BATCH_SIZE,
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
        if job_id:
            store.update_job(
                job_id,
                progress={
                    "stage": "fast_layer",
                    "message": "檢查模擬器、遊戲前景與快速規則",
                },
                performance=performance,
            )
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
        performance["fast_timings"] = fast_result.get("timings", [])
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
            performance_analysis = analyze_performance(performance, fast_result, result)
            performance["analysis"] = performance_analysis
            result["performance"] = performance
            result["performance_analysis"] = performance_analysis
            if job_id:
                store.update_job(
                    job_id,
                    status="done",
                    engine_used="fast-rules",
                    run_reason=result["reason"],
                    attempts=[],
                    fast_decision=fast_result,
                    performance=performance,
                    performance_analysis=performance_analysis,
                    result=_format_job_result(result))
                autotune_job = _enqueue_autotune_job(
                    job_id,
                    game,
                    agent_id,
                    performance_analysis,
                    result,
                    codex_model,
                    codex_reasoning_effort,
                )
                if autotune_job:
                    result["autotune"] = {
                        "job_id": autotune_job["id"],
                        "spawned": autotune_job.get("spawned", False),
                    }
            return result

    fast_context = fast_agent.format_fast_context(game.get("id", ""), fast_result)
    visual_context = visual_memory.format_prompt_context(game.get("id", ""))
    performance["fast_context_chars"] = len(fast_context)
    performance["visual_context_chars"] = len(visual_context)
    task_parts = split_numbered_task(task)
    performance["segments_detected"] = len(task_parts)
    performance["segmentation_requested"] = bool(segment_mode)
    performance["auto_segmentation_requested"] = bool(auto_segment)
    use_segments = (
        (segment_mode and len(task_parts) >= 2)
        or (auto_segment and len(task_parts) >= AUTO_SEGMENT_MIN_STEPS)
    )
    segment_batches = batch_task_parts(task_parts, segment_batch_size) if use_segments else []
    performance["segment_batch_size"] = segment_batch_size if use_segments else None
    performance["segment_step_batches"] = [
        {"start_step": b["start_step"], "end_step": b["end_step"]}
        for b in segment_batches
    ]
    performance["mode"] = "segmented" if use_segments else "single"
    performance["segments_total"] = len(segment_batches) if use_segments else 1
    segment_timeout = int(segment_timeout or DEFAULT_SEGMENT_TIMEOUT_SECONDS)
    segment_timeout = max(60, min(int(timeout), segment_timeout))
    if not use_segments:
        segment_timeout = timeout
    performance["segment_timeout_seconds"] = segment_timeout if use_segments else None
    prompt = build_agent_prompt(
        game, task, fast_context=fast_context, visual_context=visual_context)
    performance["prompt_chars"] = len(prompt)
    if job_id:
        store.update_job(
            job_id,
            progress={
                "stage": "codex_handoff",
                "message": "已完成本地檢查，Codex 判斷中",
                "fast_handoff_reason": performance.get("fast_handoff_reason", ""),
                "prompt_chars": performance["prompt_chars"],
            },
            performance=performance,
        )
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
            for index, batch in enumerate(segment_batches, 1):
                part = batch["task"]
                segment_task = _segment_task(
                    task, part, index, len(segment_batches), summaries)
                segment_prompt = build_agent_prompt(
                    game, segment_task,
                    fast_context=fast_context,
                    visual_context=visual_context)
                segment_info = {
                    "index": index,
                    "start_step": batch["start_step"],
                    "end_step": batch["end_step"],
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
                            "total_segments": len(segment_batches),
                            "start_step": batch["start_step"],
                            "end_step": batch["end_step"],
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
                    f"## Segment {index}/{len(segment_batches)} "
                    f"(steps {batch['start_step']}-{batch['end_step']})\n\n"
                    f"{part}\n\n{output}".strip())
                if not segment_result.get("ok"):
                    failed_reason = (
                        f"segment {index}/{len(segment_batches)} failed: "
                        f"{segment_result.get('reason', '')}")
                    break
            ok = not failed_reason
            result = {
                "engine_used": "codex-segmented",
                "ok": ok,
                "output": "\n\n".join(outputs),
                "attempts": attempts,
                "reason": (
                    f"completed {len(segment_batches)} segments"
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
    visual_fast_rules_merge = (visual_memory_merge or {}).get("fast_rules")
    learned_lessons = extract_skill_lessons(result.get("output", ""))
    skill_lessons_update = None
    if learned_lessons:
        skill_lessons_update = store.append_skill_lessons(
            game.get("id", ""),
            learned_lessons,
            source=f"run_agent:{job_id or 'manual'}",
        )

    performance_analysis = analyze_performance(
        performance,
        fast_result,
        result,
        fast_rules_merge=fast_rules_merge,
        visual_memory_merge=visual_memory_merge,
    )
    performance["analysis"] = performance_analysis
    result["performance_analysis"] = performance_analysis

    if job_id:
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            engine_used=result.get("engine_used"),
            run_reason=result.get("reason", ""),
            attempts=_summarize_attempts(result.get("attempts", [])),
            fast_decision=fast_result,
            fast_rules=fast_rules_merge,
            fast_rules_from_visual_memory=visual_fast_rules_merge,
            visual_memory=visual_memory_merge,
            skill_lessons=skill_lessons_update,
            performance=performance,
            performance_analysis=performance_analysis,
            progress=None,
            result=_format_job_result(result),
            error_trace=(result.get("traceback") or "")[:4000] or None)
        autotune_job = _enqueue_autotune_job(
            job_id,
            game,
            agent_id,
            performance_analysis,
            result,
            codex_model,
            codex_reasoning_effort,
        )
        if autotune_job:
            result["autotune"] = {
                "job_id": autotune_job["id"],
                "spawned": autotune_job.get("spawned", False),
            }
    if fast_rules_merge:
        result["fast_rules"] = fast_rules_merge
    if visual_fast_rules_merge:
        result["fast_rules_from_visual_memory"] = visual_fast_rules_merge
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
    ap.add_argument("--segment", action="store_true",
                    help="啟用條列任務分段；預設停用以避免多次 Codex 啟動拖慢操作")
    ap.add_argument("--auto-segment", action="store_true",
                    help="條列任務達到一定步數時自動分段")
    ap.add_argument("--no-segment", action="store_true",
                    help="相容舊參數：維持停用分段")
    ap.add_argument("--segment-timeout", type=int,
                    default=DEFAULT_SEGMENT_TIMEOUT_SECONDS,
                    help="每個分段最多等待秒數，預設 600")
    ap.add_argument("--segment-batch-size", type=int,
                    default=DEFAULT_SEGMENT_BATCH_SIZE,
                    help="每段包含幾個編號步驟，預設 2，最多 4")
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
                    segment_mode=args.segment and not args.no_segment,
                    auto_segment=args.auto_segment and not args.no_segment,
                    segment_timeout=args.segment_timeout,
                    segment_batch_size=args.segment_batch_size,
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
