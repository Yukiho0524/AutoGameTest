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
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # game reports are Chinese; avoid cp950 crash
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import store, adb, fast_agent, visual_memory, testcases, player_reports  # noqa: E402
import ai_runner  # noqa: E402

DEFAULT_SEGMENT_TIMEOUT_SECONDS = 600
AUTO_SEGMENT_MIN_STEPS = 5
DEFAULT_SEGMENT_BATCH_SIZE = 2
DEFAULT_VISUAL_MAX_TURNS = None
DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS = 180
DEFAULT_AUTONOMOUS_TASK = """自主探索模式：
1. 進入遊戲後自行觀察目前畫面，辨識主選單、活動、任務、商店、角色、關卡、設定等入口。
2. 優先探索低風險 UI，記錄每一輪看到的畫面、可點擊入口、轉場結果與卡住點。
3. 遇到登入、PVP、未知或不確定畫面時不要立刻停止，先回報觀察，再嘗試等待、返回上一層或探索其他低風險入口。
4. 不代輸帳密、不按第三方授權、不購買付費商品、不開始會影響真人玩家的排位或匹配。
5. 到達使用者設定的時間限制、整理出主要 UI 地圖，或碰到必須真人決策的硬邊界時，回報本次探索摘要。"""
COMMON_MOBILE_CONTROLS_SKILL = ".codex/skills/mobile-game-controls/SKILL.md"
_CREATE_NO_WINDOW = 0x08000000
LOG_DIR = os.path.join(ROOT, "data", "logs")
ARTIFACT_DIR = os.path.join(ROOT, "data", "artifacts")


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


def _segment_task(part: str, index: int, total: int,
                  previous_summaries: list[str],
                  next_preview: str = "") -> str:
    previous = "\n".join(
        f"- {summary[:500]}" for summary in previous_summaries[-3:]
        if summary.strip())
    if not previous:
        previous = "- 尚無"
    if not next_preview:
        next_preview = "- 無"
    return f"""這是一個短 checkpoint 分段任務。只有「目前可執行段落」可以操作，完成後立刻停止並回報。

目前可執行段落（{index}/{total}）：
{part}

已完成段落摘要：
{previous}

後續段落預覽（只供辨識流程，禁止現在執行）：
{next_preview}

執行要求：
- 本段開始時先重新截圖確認目前畫面，不要依賴上一段截圖。
- 只做目前段落需要的低風險操作；不要操作下一段。
- 如果完成目前段落後看到下一段入口，也必須停止，不要順手點下一步。
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
    writeback = result.get("testcase_writeback")
    writeback_text = ""
    if isinstance(writeback, dict):
        if writeback.get("ok"):
            writeback_text = (
                "\n\n[Excel 回寫] "
                f"{writeback.get('message') or '已嘗試回寫。'}"
            )
        else:
            writeback_text = (
                "\n\n[Excel 回寫失敗] "
                f"{writeback.get('error') or writeback.get('message') or '未知錯誤'}"
            )
    report = result.get("autonomous_report")
    report_text = ""
    if isinstance(report, dict):
        if report.get("ok"):
            report_text = (
                "\n\n[自主探索玩家回饋 Excel] "
                f"{report.get('relative_path') or report.get('path') or report.get('name')}"
            )
        elif report.get("error"):
            report_text = f"\n\n[自主探索報告失敗] {report.get('error')}"
    if output:
        if engine == "codex-segmented" and len(output) > 2800:
            body_limit = max(800, 3000 - len(head) - len(writeback_text) - len(report_text) - 22)
            return (
                head + "\n\n...[前段分段輸出已省略]\n"
                + output[-body_limit:] + writeback_text + report_text
            )[:3000]
        body_limit = max(800, 3000 - len(head) - len(writeback_text) - len(report_text) - 4)
        body = output if len(output) <= body_limit else output[:body_limit] + "\n...[輸出已截斷]"
        return (head + "\n\n" + body + writeback_text + report_text)[:3000]
    return (head + writeback_text + report_text)[:3000]


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


def _clip_text(text: str, limit: int = 12000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n...（內容過長，已截斷）"


def _png_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            data = f.read(24)
        if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except OSError:
        return None
    return None


def _visual_artifact_dir(job_id: str | None) -> str:
    run_id = job_id or datetime.now().strftime("manual_visual_%Y%m%d_%H%M%S")
    path = os.path.join(ARTIFACT_DIR, str(run_id))
    os.makedirs(path, exist_ok=True)
    return path


def _write_png(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _fast_visual_device_detail(emulator: str, serial: str) -> dict:
    adb_path = adb.adb_path_for(emulator)
    detail = {
        "emulator": emulator,
        "serial": serial,
        "adb_path": adb_path,
        "adb_exists": os.path.isfile(adb_path),
    }
    try:
        detail["devices"] = adb.devices_text(emulator)
    except Exception as e:
        detail["devices_error"] = str(e)
    return detail


def _prepare_fast_visual_device(game: dict, serial: str, emulator: str,
                                package: str, job_id: str | None,
                                mode_label: str) -> tuple[bool, dict]:
    lc = game.get("launch", {})
    try:
        instance = int(lc.get("instance", 0) or 0)
    except (TypeError, ValueError):
        instance = 0
    detail = _fast_visual_device_detail(emulator, serial)
    backend_available = adb.available(emulator)
    detail["backend_available"] = backend_available
    if not detail.get("adb_exists"):
        detail["reason"] = "找不到模擬器 ADB"
        return False, detail

    if job_id:
        store.update_job(
            job_id,
            progress={
                "stage": "fast_visual_preflight",
                "message": f"{mode_label}檢查模擬器與 ADB 連線",
                "emulator": emulator,
                "serial": serial,
            },
        )
    ready = adb.adb_ready(serial, emulator)
    detail["ready_before_launch"] = ready
    if not ready:
        launched = adb.launch_instance(instance, emulator) if backend_available else False
        detail["launch_instance"] = launched
        wait_seconds = 60 if backend_available else 10
        detail["ready_wait_seconds"] = wait_seconds
        deadline = time.perf_counter() + wait_seconds
        while time.perf_counter() < deadline:
            time.sleep(2)
            if adb.adb_ready(serial, emulator):
                ready = True
                break
        detail["ready_after_launch"] = ready
    if not ready:
        detail.update(_fast_visual_device_detail(emulator, serial))
        detail["reason"] = (
            "模擬器未 ready 或 serial 無法連線；且啟動器不可用，無法自動啟動"
            if not backend_available
            else "模擬器未 ready 或 serial 無法連線"
        )
        return False, detail

    foreground = ""
    try:
        foreground = adb.current_package(serial, emulator)
    except Exception as e:
        detail["foreground_error"] = str(e)
    detail["foreground_before_launch"] = foreground
    if package and foreground != package:
        if job_id:
            store.update_job(
                job_id,
                progress={
                    "stage": "fast_visual_preflight",
                    "message": f"{mode_label}啟動遊戲 App",
                    "package": package,
                },
            )
        launched_app = adb.launch_app(serial, package, emulator)
        detail["launch_app"] = launched_app
        time.sleep(5.0)
        try:
            detail["foreground_after_launch"] = adb.current_package(serial, emulator)
        except Exception as e:
            detail["foreground_after_launch_error"] = str(e)
        if not launched_app:
            detail["reason"] = f"遊戲 App 啟動失敗：{package}"
            return False, detail
    return True, detail


def _format_fast_visual_failure(prefix: str, detail: dict) -> str:
    parts = [prefix]
    if detail.get("reason"):
        parts.append(str(detail["reason"]))
    for key, label in (
        ("emulator", "emulator"),
        ("serial", "serial"),
        ("adb_path", "adb"),
        ("adb_exists", "adb_exists"),
        ("stage", "stage"),
        ("rc", "rc"),
        ("stderr", "stderr"),
        ("devices", "devices"),
        ("foreground_before_launch", "foreground"),
        ("foreground_after_launch", "foreground_after_launch"),
    ):
        value = detail.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{label}={value}")
    return "；".join(parts)[:1200]


def build_visual_turn_prompt(game: dict, task: str, screenshot_path: str,
                             turn: int, max_turns: int | None,
                             state_summary: str = "",
                             extra_skill_context: str = "",
                             autonomous_mode: bool = False) -> str:
    """Small stateless prompt for one screenshot decision."""
    skill = _clip_text(_read(game.get("skill_path", "")), 14000)
    visual_memory_context = _clip_text(
        visual_memory.format_prompt_context(game.get("id", ""), limit=6), 4500)
    extra_skill_context = _clip_text(extra_skill_context, 6000)
    size = _png_size(screenshot_path)
    size_text = f"{size[0]}x{size[1]}" if size else "unknown"
    lc = game.get("launch", {})
    package = lc.get("package", "")
    mode_name = "自主探索模式" if autonomous_mode else "快速逐圖模式"
    task_text = (task or DEFAULT_AUTONOMOUS_TASK).strip()
    turn_limit_text = "不限（以總時間為準）" if max_turns is None else str(max_turns)
    extra_block = (
        f"\n# QA 系統理解 Skill\n{extra_skill_context}\n"
        if extra_skill_context else ""
    )
    visual_memory_block = (
        f"\n# 圖片記憶與已知安全畫面\n{visual_memory_context}\n"
        if visual_memory_context else ""
    )
    if autonomous_mode:
        actions = """- `tap`: 需要 `x`, `y`，使用像素座標，僅用於明顯低風險入口。
- `swipe`: 需要 `x1`, `y1`, `x2`, `y2`, 可選 `duration_ms`。
- `wait`: 需要 `seconds`，用於 loading/轉場/觀察。
- `back`: Android 返回鍵，用於退回上一層或離開不適合深入的頁面。
- `launch_app`: 重新啟動遊戲 package（目前 package: `{package}`）。
- `done`: 本次探索已形成足夠摘要，或達到自然收尾點。
- `stop`: 只在需要輸入帳密、第三方授權、付費購買、不可逆破壞操作，或已離開遊戲且無法返回時使用。""".format(package=package)
        rules = """- 你的目標是自己探索遊戲，不是完成使用者指定步驟。
- 每輪只做 1 個動作，並在 JSON 裡寫清楚 `observation` 與 `learned`。
- 遇到登入畫面不要直接停；先記錄畫面。若有「稍後、略過、返回、關閉」等安全入口可嘗試；不得輸入帳密、驗證碼或點第三方授權。
- 遇到 PVP / 對戰入口可以瀏覽與記錄，不要開始排位、匹配或會影響真人玩家的對戰。
- 不確定時不要直接停；優先 `wait`、`back`、或點擊明顯低風險的導覽入口。
- 付費購買、消耗稀有資源、刪除資料、改帳號設定等不可逆行為不可執行。
- 座標必須是目前截圖上的像素座標。"""
        example_reason = "探索這個低風險入口，確認它對應的功能與轉場"
    else:
        actions = """- `tap`: 需要 `x`, `y`，使用像素座標。
- `swipe`: 需要 `x1`, `y1`, `x2`, `y2`, 可選 `duration_ms`。
- `wait`: 需要 `seconds`，用於 loading/轉場。
- `back`: Android 返回鍵，用於退回上一層。
- `launch_app`: 重新啟動遊戲 package（目前 package: `{package}`）。
- `done`: 任務完成。
- `stop`: 遇到登入、付費、轉蛋、PVP、未知高風險或無法判斷。""".format(package=package)
        rules = """- 每輪只做 1 個低風險動作；不要規劃多步連點。
- 若畫面已達成任務，回 `done`。
- 若正在 loading，優先 `wait` 2~8 秒。
- 不確定時回 `stop`，不要猜測高風險操作。
- 座標必須是目前截圖上的像素座標。"""
        example_reason = "為什麼這一步安全且符合任務"
    return f"""你是 AutoGameTest 的「{mode_name}」判斷器。這是一個全新的短對話；不要依賴任何舊上下文，只使用本 prompt、遊戲 Skill 與這張截圖。

# 遊戲
{game.get('name', game.get('id', ''))}

# 遊戲 Skill
{skill or '（尚無 skill，請保守判斷）'}
{visual_memory_block}
{extra_block}
# 任務
{task_text}

# 目前輪次與狀態
- turn: {turn}/{turn_limit_text}
- 上一輪摘要：{state_summary or '無，這是第一輪或剛交接。'}

# 最新截圖
- path: `{screenshot_path}`
- size: {size_text}
請直接檢視這張圖片後決定下一步。只允許回傳一個 JSON 決策，不要實際執行 ADB 指令。

# 可用動作
{actions}

# 判斷規則
{rules}

# 回覆格式
只回下列區塊，不要加其他文字：
AUTOGAMETEST_VISUAL_STEP:
```json
{{
  "status": "continue",
  "action": "tap",
  "x": 100,
  "y": 200,
  "reason": "{example_reason}",
  "observation": "目前畫面看到的 UI、文字、狀態或風險",
  "player_feedback": "以一般遊戲玩家角度，這一畫面的直覺感受、吸引力或困惑點",
  "learned": "這一輪可沉澱的遊戲知識或測試回饋",
  "next_state": "執行後預期進入的簡短狀態"
}}
```

若完成：
```json
{{"status":"done","reason":"完成證據","next_state":"done"}}
```
"""


def extract_visual_step(text: str) -> dict | None:
    raw = str(text or "")
    marker = "AUTOGAMETEST_VISUAL_STEP"
    candidates: list[str] = []
    idx = raw.find(marker)
    if idx >= 0:
        candidates.append(raw[idx + len(marker):])
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```",
                                 raw, flags=re.DOTALL | re.IGNORECASE))
    obj_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if obj_match:
        candidates.append(obj_match.group(0))
    for candidate in candidates:
        text = str(candidate or "").strip(" \t\r\n:：`")
        if text.startswith("json"):
            text = text[4:].strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
            text = text.split("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _int_field(data: dict, key: str, default: int = 0) -> int:
    try:
        return int(round(float(data.get(key, default))))
    except (TypeError, ValueError):
        return default


def execute_visual_decision(decision: dict, serial: str, emulator: str,
                            package: str) -> tuple[bool, str]:
    status = str(decision.get("status", "continue") or "continue").lower()
    action = str(decision.get("action", "") or "").lower()
    if status == "done" or action == "done":
        return True, "done"
    if status == "stop" or action == "stop":
        return False, "stop"
    if action == "tap":
        ok = adb.tap(serial, _int_field(decision, "x"), _int_field(decision, "y"), emulator)
        time.sleep(0.7)
        return ok, "tap"
    if action == "swipe":
        ok = adb.swipe(
            serial,
            _int_field(decision, "x1"),
            _int_field(decision, "y1"),
            _int_field(decision, "x2"),
            _int_field(decision, "y2"),
            max(100, min(5000, _int_field(decision, "duration_ms", 300))),
            emulator,
        )
        time.sleep(0.7)
        return ok, "swipe"
    if action == "back":
        ok = adb.keyevent(serial, "BACK", emulator)
        time.sleep(0.7)
        return ok, "back"
    if action == "wait":
        seconds = max(0.5, min(20.0, float(decision.get("seconds", 3) or 3)))
        time.sleep(seconds)
        return True, f"wait {seconds:.1f}s"
    if action == "launch_app":
        if not package:
            return False, "launch_app missing package"
        ok = adb.launch_app(serial, package, emulator)
        time.sleep(5.0)
        return ok, "launch_app"
    return False, f"unsupported action: {action or '(empty)'}"


def build_agent_prompt(game: dict, task: str, fast_context: str = "",
                       visual_context: str = "",
                       extra_skill_context: str = "") -> str:
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
    extra_skill_block = (
        f"# QA 系統理解 Skill\n{extra_skill_context}"
        if extra_skill_context else ""
    )

    return f"""你是一位遊戲玩家，代替使用者操作《{game.get('name','')}》完成指定任務。

# 角色與守則
{persona or '（無專屬 persona 檔，請以謹慎的遊戲玩家身分操作）'}

# 遊戲知識庫（Skill）
{skill or '（尚無 skill，請先謹慎探索並記錄）'}

{extra_skill_block}

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


def run_fast_visual_mode(game: dict, task: str, job_id: str | None,
                         timeout: int, model: str, reasoning_effort: str,
                         extra_skill_context: str = "",
                         max_turns: int | None = DEFAULT_VISUAL_MAX_TURNS,
                         turn_timeout: int = DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS,
                         autonomous_mode: bool = False) -> dict:
    engine_used = "codex-autonomous-visual" if autonomous_mode else "codex-fast-visual"
    mode_label = "自主探索模式" if autonomous_mode else "快速逐圖模式"
    if game.get("control") != "emulator":
        return {
            "engine_used": engine_used,
            "ok": False,
            "output": "",
            "attempts": [],
            "reason": f"{mode_label}目前只支援 Android 模擬器 Agent",
        }
    adb.reload_config_paths()
    lc = game.get("launch", {})
    emulator = adb.normalize_emulator(lc.get("emulator", "ldplayer"))
    try:
        instance = int(lc.get("instance", 0) or 0)
    except (TypeError, ValueError):
        instance = 0
    serial = lc.get("serial") or adb.serial_for(instance, emulator)
    package = lc.get("package", "")
    artifact_dir = _visual_artifact_dir(job_id)
    attempts: list[dict] = []
    turns: list[dict] = []
    outputs: list[str] = []
    state_summary = ""
    sandbox = "danger-full-access"
    total_timeout = max(60, int(timeout or 3600))
    deadline = time.perf_counter() + total_timeout
    timeout_limit_reason = f"達到{mode_label}時間限制 {total_timeout} 秒"
    if autonomous_mode or max_turns in (None, "", 0):
        max_turns = None
    else:
        max_turns = max(1, min(1000, int(max_turns)))
    turn_timeout = max(60, int(turn_timeout or DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS))
    turn_timeout = min(total_timeout, turn_timeout)

    ok_device, device_detail = _prepare_fast_visual_device(
        game, serial, emulator, package, job_id, mode_label)
    if not ok_device:
        reason = _format_fast_visual_failure(f"{mode_label}啟動前檢查失敗", device_detail)
        turns.append({
            "turn": 0,
            "status": "error",
            "reason": reason,
            "diagnostics": device_detail,
        })
        return {
            "engine_used": engine_used,
            "ok": False,
            "output": reason,
            "attempts": attempts,
            "reason": reason,
            "visual_turns": turns,
            "artifact_dir": artifact_dir,
        }

    turn = 1
    while max_turns is None or turn <= max_turns:
        started = time.perf_counter()
        remaining_seconds = deadline - started
        if remaining_seconds <= 1:
            break
        current_turn_timeout = max(1, min(turn_timeout, int(remaining_seconds)))
        turn_limit_text = "不限" if max_turns is None else str(max_turns)
        if job_id:
            store.update_job(
                job_id,
                progress={
                    "stage": "fast_visual",
                    "message": (
                        f"{mode_label}第 {turn}/{turn_limit_text} 輪截圖判斷"
                        f"（剩餘約 {int(remaining_seconds)} 秒）"
                    ),
                    "turn": turn,
                    "max_turns": max_turns,
                    "remaining_seconds": int(remaining_seconds),
                },
            )
        png, screenshot_detail = adb.screenshot_with_detail(serial, emulator)
        if not png:
            screenshot_detail.update({
                "devices": device_detail.get("devices", ""),
                "foreground_before_launch": device_detail.get("foreground_before_launch", ""),
                "foreground_after_launch": device_detail.get("foreground_after_launch", ""),
            })
            reason = _format_fast_visual_failure("截圖失敗", screenshot_detail)
            turns.append({
                "turn": turn,
                "status": "error",
                "reason": reason,
                "diagnostics": screenshot_detail,
            })
            return {
                "engine_used": engine_used,
                "ok": False,
                "output": (("\n".join(outputs) + "\n") if outputs else "") + reason,
                "attempts": attempts,
                "reason": reason,
                "visual_turns": turns,
                "artifact_dir": artifact_dir,
            }
        screenshot_path = _write_png(
            os.path.join(artifact_dir, f"visual_{turn:03d}.png"), png)
        prompt = build_visual_turn_prompt(
            game, task, screenshot_path, turn, max_turns,
            state_summary=state_summary,
            extra_skill_context=extra_skill_context,
            autonomous_mode=autonomous_mode,
        )
        result = ai_runner.run_with_fallback(
            prompt,
            cwd=ROOT,
            timeout=current_turn_timeout,
            engine="codex",
            fallback=False,
            codex_sandbox=sandbox,
            codex_model=model,
            codex_reasoning_effort=reasoning_effort,
        )
        for attempt in result.get("attempts", []):
            item = dict(attempt)
            item["visual_turn"] = turn
            attempts.append(item)
        output = result.get("output", "")
        decision = extract_visual_step(output)
        elapsed = round(time.perf_counter() - started, 3)
        if not result.get("ok") or not decision:
            reason = result.get("reason") or "Codex 未回傳可解析的逐圖 JSON"
            timed_out = any(
                "timeout after" in str(a.get("detail") or "").lower()
                for a in result.get("attempts", [])
            )
            if autonomous_mode and timed_out and time.perf_counter() >= deadline - 1:
                turns.append({
                    "turn": turn,
                    "status": "timeout",
                    "screenshot": screenshot_path,
                    "elapsed_seconds": elapsed,
                    "reason": timeout_limit_reason,
                })
                outputs.append(
                    f"Turn {turn}: timeout - {timeout_limit_reason}\n{screenshot_path}".strip())
                return {
                    "engine_used": engine_used,
                    "ok": True,
                    "output": "\n\n".join(outputs),
                    "attempts": attempts,
                    "reason": timeout_limit_reason,
                    "visual_turns": turns,
                    "artifact_dir": artifact_dir,
                }
            if autonomous_mode and timed_out:
                timeout_reason = (
                    f"第 {turn} 輪 Codex 判斷超過 {current_turn_timeout} 秒，"
                    "未執行操作；保留截圖並繼續下一輪。"
                )
                turns.append({
                    "turn": turn,
                    "status": "timeout",
                    "screenshot": screenshot_path,
                    "elapsed_seconds": elapsed,
                    "reason": timeout_reason,
                })
                outputs.append(
                    f"Turn {turn}: timeout - {timeout_reason}\n{screenshot_path}".strip())
                state_summary = (
                    f"上一輪在 {screenshot_path} 判斷逾時，沒有執行操作；"
                    "本輪請優先比對圖片記憶，若是已知安全教學/載入畫面，"
                    "用最短 JSON 決策回覆。"
                )
                turn += 1
                continue
            turns.append({
                "turn": turn,
                "status": "error",
                "screenshot": screenshot_path,
                "elapsed_seconds": elapsed,
                "reason": reason,
            })
            outputs.append(
                f"Turn {turn}: error - {reason}\n{screenshot_path}\n{output}".strip())
            return {
                "engine_used": engine_used,
                "ok": False,
                "output": "\n\n".join(outputs),
                "attempts": attempts,
                "reason": reason,
                "visual_turns": turns,
                "artifact_dir": artifact_dir,
            }
        status = str(decision.get("status", "continue") or "continue").lower()
        action = str(decision.get("action", "") or "").lower()
        reason = str(decision.get("reason", "") or "").strip()
        observation = str(decision.get("observation", "") or "").strip()
        player_feedback = str(
            decision.get("player_feedback")
            or decision.get("feeling")
            or decision.get("player_feeling")
            or ""
        ).strip()
        learned = str(decision.get("learned", "") or "").strip()
        next_state = str(decision.get("next_state", "") or "").strip()
        if status == "done" or action == "done":
            turns.append({
                "turn": turn,
                "status": "done",
                "action": "done",
                "screenshot": screenshot_path,
                "elapsed_seconds": elapsed,
                "reason": reason,
                "observation": observation,
                "player_feedback": player_feedback,
                "learned": learned,
                "next_state": next_state,
            })
            outputs.append(
                f"Turn {turn}: done - {reason}"
                + (f"\n觀察：{observation}" if observation else "")
                + (f"\n玩家感受：{player_feedback}" if player_feedback else "")
                + (f"\n學到：{learned}" if learned else ""))
            return {
                "engine_used": engine_used,
                "ok": True,
                "output": "\n".join(outputs),
                "attempts": attempts,
                "reason": reason or f"{mode_label}完成",
                "visual_turns": turns,
                "artifact_dir": artifact_dir,
            }
        if status == "stop" or action == "stop":
            turns.append({
                "turn": turn,
                "status": "stopped",
                "action": "stop",
                "screenshot": screenshot_path,
                "elapsed_seconds": elapsed,
                "reason": reason,
                "observation": observation,
                "player_feedback": player_feedback,
                "learned": learned,
                "next_state": next_state,
                "decision": decision,
            })
            outputs.append(
                f"Turn {turn}: stop - {reason}"
                + (f"\n觀察：{observation}" if observation else "")
                + (f"\n玩家感受：{player_feedback}" if player_feedback else "")
                + (f"\n學到：{learned}" if learned else ""))
            return {
                "engine_used": engine_used,
                "ok": bool(autonomous_mode),
                "output": "\n".join(outputs),
                "attempts": attempts,
                "reason": reason or f"{mode_label}停止",
                "visual_turns": turns,
                "artifact_dir": artifact_dir,
            }
        ok, exec_detail = execute_visual_decision(decision, serial, emulator, package)
        turn_info = {
            "turn": turn,
            "status": "done" if ok else "error",
            "action": action,
            "screenshot": screenshot_path,
            "elapsed_seconds": elapsed,
            "reason": reason,
            "observation": observation,
            "player_feedback": player_feedback,
            "learned": learned,
            "next_state": next_state,
            "execute_detail": exec_detail,
            "decision": decision,
        }
        turns.append(turn_info)
        outputs.append(
            f"Turn {turn}: {action or '(none)'} -> {exec_detail}; "
            f"{reason}".strip()
            + (f"\n觀察：{observation}" if observation else "")
            + (f"\n玩家感受：{player_feedback}" if player_feedback else "")
            + (f"\n學到：{learned}" if learned else ""))
        if not ok:
            return {
                "engine_used": engine_used,
                "ok": False,
                "output": "\n".join(outputs),
                "attempts": attempts,
                "reason": exec_detail,
                "visual_turns": turns,
                "artifact_dir": artifact_dir,
            }
        state_summary = (
            f"上一輪執行 {action}，結果 {exec_detail}。"
            f"觀察：{observation[:120]}。"
            f"原因：{reason[:120]}。下一狀態：{next_state[:120]}。")
        turn += 1
    limit_reason = (
        timeout_limit_reason
        if max_turns is None
        else f"達到{mode_label}最大輪數 {max_turns}"
    )
    return {
        "engine_used": engine_used,
        "ok": bool(autonomous_mode),
        "output": "\n".join(outputs),
        "attempts": attempts,
        "reason": limit_reason,
        "visual_turns": turns,
        "artifact_dir": artifact_dir,
    }


def run_agent(agent_id=None, game_id=None, task=None, job_id=None,
              engine="codex", fallback=False, timeout=3600,
              fast_mode=True, fast_steps=8,
              codex_model: str | None = None,
              codex_reasoning_effort: str | None = None,
              segment_mode: bool = False,
              auto_segment: bool = False,
              segment_timeout: int | None = None,
              segment_batch_size: int = DEFAULT_SEGMENT_BATCH_SIZE,
              fast_visual_mode: bool = False,
              visual_max_turns: int | None = DEFAULT_VISUAL_MAX_TURNS,
              visual_turn_timeout: int = DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS,
              autonomous_mode: bool = False,
              extra_skill_path: str = "",
              print_only=False) -> dict:
    total_start = time.perf_counter()
    agent = None
    if agent_id:
        agent = store.get_agent(agent_id)
        if not agent:
            return {"ok": False, "error": f"agent 不存在: {agent_id}"}
        game_id = agent.get("game_id")
        task = task or agent.get("prompt", "")
    job_payload = {}
    if job_id:
        current_job = store.get_job(job_id) or {}
        job_payload = current_job.get("payload") or {}
    autonomous_mode = bool(
        autonomous_mode
        or (agent or {}).get("autonomous_mode")
        or job_payload.get("autonomous_mode"))
    if autonomous_mode and not str(task or "").strip():
        task = DEFAULT_AUTONOMOUS_TASK
    if not game_id:
        return {"ok": False, "error": "缺少 game_id / agent"}
    game = store.get_game(game_id)
    if not game:
        return {"ok": False, "error": f"遊戲不存在: {game_id}"}
    if not task:
        return {"ok": False, "error": "缺少任務內容"}

    fast_visual_mode = bool(
        autonomous_mode
        or
        fast_visual_mode
        or (agent or {}).get("fast_visual_mode")
        or job_payload.get("fast_visual_mode"))
    if autonomous_mode:
        visual_max_turns = None
    else:
        raw_visual_max_turns = job_payload.get("visual_max_turns", visual_max_turns)
        if raw_visual_max_turns in (None, "", 0):
            visual_max_turns = None
        else:
            try:
                visual_max_turns = int(raw_visual_max_turns)
            except (TypeError, ValueError):
                visual_max_turns = DEFAULT_VISUAL_MAX_TURNS
    try:
        visual_turn_timeout = int(
            job_payload.get("visual_turn_timeout") or visual_turn_timeout)
    except (TypeError, ValueError):
        visual_turn_timeout = DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS

    codex_model, codex_reasoning_effort = _resolve_codex_settings(
        codex_model, codex_reasoning_effort)
    performance = _base_metrics(
        game, task, codex_model, codex_reasoning_effort)
    performance["autonomous_mode"] = bool(autonomous_mode)
    extra_skill_context = _read(extra_skill_path) if extra_skill_path else ""
    if extra_skill_context:
        performance["extra_skill_path"] = extra_skill_path
        performance["extra_skill_chars"] = len(extra_skill_context)

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
    if fast_mode and not autonomous_mode and game.get("control") == "emulator" and not print_only:
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

    if fast_visual_mode and not print_only:
        performance["mode"] = "autonomous-visual" if autonomous_mode else "fast-visual"
        performance["segments_total"] = None if autonomous_mode else visual_max_turns
        performance["segment_timeout_seconds"] = visual_turn_timeout
        performance["visual_limit_mode"] = (
            "time" if autonomous_mode or visual_max_turns is None else "turns")
        performance["visual_total_timeout_seconds"] = int(timeout)
        performance["visual_max_turns"] = visual_max_turns
        performance["visual_turn_timeout_seconds"] = visual_turn_timeout
        if job_id:
            store.update_job(
                job_id,
                progress={
                    "stage": "fast_visual",
                    "message": (
                        "自主探索模式啟動：AI 逐圖觀察並回饋畫面"
                        if autonomous_mode
                        else "快速逐圖模式啟動：每張截圖使用新的 Codex 對話"
                    ),
                    "fast_handoff_reason": performance.get("fast_handoff_reason", ""),
                },
                performance=performance,
            )
        try:
            result = run_fast_visual_mode(
                game,
                task,
                job_id=job_id,
                timeout=timeout,
                model=codex_model,
                reasoning_effort=codex_reasoning_effort,
                extra_skill_context=extra_skill_context,
                max_turns=visual_max_turns,
                turn_timeout=visual_turn_timeout,
                autonomous_mode=autonomous_mode,
            )
        except Exception as e:
            result = {
                "engine_used": "codex-fast-visual",
                "ok": False,
                "output": "",
                "attempts": [],
                "reason": f"fast visual crashed: {e}",
                "traceback": traceback.format_exc(),
            }
        performance["codex_seconds"] = round(sum(
            float(a.get("elapsed_seconds") or 0)
            for a in result.get("attempts", [])
        ), 3)
        performance["segments_completed"] = len(result.get("visual_turns", []))
        performance["visual_turns"] = result.get("visual_turns", [])
        if result.get("artifact_dir"):
            performance["artifact_dir"] = result["artifact_dir"]
            try:
                performance["artifact_png_count"] = len([
                    name for name in os.listdir(result["artifact_dir"])
                    if name.lower().endswith(".png")
                ])
            except OSError:
                pass
    else:
        prompt = build_agent_prompt(
            game, task, fast_context=fast_context, visual_context=visual_context,
            extra_skill_context=extra_skill_context)
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
                    upcoming = segment_batches[index:index + 2]
                    next_preview = "\n".join(
                        f"- steps {item['start_step']}-{item['end_step']}: "
                        f"{' / '.join(str(item['task']).splitlines())[:180]}"
                        for item in upcoming
                    )
                    segment_task = _segment_task(
                        part, index, len(segment_batches), summaries, next_preview)
                    segment_prompt = build_agent_prompt(
                        game, segment_task,
                        fast_context=fast_context,
                        visual_context=visual_context,
                        extra_skill_context=extra_skill_context)
                    segment_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    segment_info = {
                        "index": index,
                        "start_step": batch["start_step"],
                        "end_step": batch["end_step"],
                        "task": part[:500],
                        "prompt_chars": len(segment_prompt),
                        "timeout_seconds": segment_timeout,
                        "started_at": segment_started_at,
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
                                "segment_started_at": segment_started_at,
                                "segment_timeout_seconds": segment_timeout,
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
    if performance.get("mode") not in ("fast-visual", "autonomous-visual"):
        performance["artifact_png_count"] = _count_artifact_pngs(fast_result)
    result["performance"] = performance

    autonomous_report = None
    if autonomous_mode:
        try:
            autonomous_report = player_reports.write_autonomous_player_report(
                game, job_id or "manual", result, performance)
        except Exception as e:
            autonomous_report = {"ok": False, "error": str(e)}
        result["autonomous_report"] = autonomous_report

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

    testcase_writeback = None
    if job_id and job_payload.get("source") == "testcase":
        testcase_name = job_payload.get("testcase_name", "")
        try:
            testcase_writeback = testcases.write_testcase_results_from_output(
                testcase_name, result.get("output", ""))
        except Exception as e:
            testcase_writeback = {
                "ok": False,
                "updated": 0,
                "parsed": 0,
                "missing": [],
                "error": str(e),
            }
        result["testcase_writeback"] = testcase_writeback

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
            autonomous_report=autonomous_report,
            testcase_writeback=testcase_writeback,
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
    if autonomous_report:
        result["autonomous_report"] = autonomous_report
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
    ap.add_argument("--fast-visual", action="store_true",
                    help="啟用快速逐圖模式：每張截圖使用新的 Codex 最小對話")
    ap.add_argument("--autonomous", action="store_true",
                    help="啟用自主探索模式：允許任務空白，AI 自行探索並回饋畫面")
    ap.add_argument("--visual-max-turns", type=int,
                    default=DEFAULT_VISUAL_MAX_TURNS,
                    help="快速逐圖模式最多輪數；未指定或 0 代表不限，以總 timeout 控制")
    ap.add_argument("--visual-turn-timeout", type=int,
                    default=DEFAULT_VISUAL_TURN_TIMEOUT_SECONDS,
                    help="快速逐圖模式每輪 Codex timeout 秒數，預設 180")
    ap.add_argument("--fast-steps", type=int, default=8, help="快速規則最多連續執行步數")
    ap.add_argument("--print-prompt", action="store_true", help="只組裝並印出 prompt，不執行")
    args = ap.parse_args(argv)

    agent_id, game_id, task = args.agent, args.game, args.task
    extra_skill_path = ""
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr); return 2
        p = job.get("payload", {})
        agent_id = agent_id or p.get("agent_id")
        game_id = game_id or p.get("game_id")
        task = task or p.get("prompt") or p.get("task")
        extra_skill_path = (
            p.get("testcase_system_skill_path")
            or p.get("system_skill_path")
            or ""
        )
        if p.get("fast_visual_mode"):
            args.fast_visual = True
        if p.get("autonomous_mode"):
            args.autonomous = True
        if p.get("visual_max_turns"):
            try:
                args.visual_max_turns = int(p.get("visual_max_turns"))
            except (TypeError, ValueError):
                pass
        if p.get("visual_turn_timeout"):
            try:
                args.visual_turn_timeout = int(p.get("visual_turn_timeout"))
            except (TypeError, ValueError):
                pass

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
                    fast_visual_mode=args.fast_visual,
                    visual_max_turns=args.visual_max_turns,
                    visual_turn_timeout=args.visual_turn_timeout,
                    autonomous_mode=args.autonomous,
                    extra_skill_path=extra_skill_path,
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
