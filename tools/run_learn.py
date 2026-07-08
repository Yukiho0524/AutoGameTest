"""Build or update a game's Skill file with Codex."""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import store, visual_memory  # noqa: E402
import ai_runner  # noqa: E402


def _fetch_url(url: str, limit: int = 12000) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AutoGameTest/0.1 (+local skill builder)",
            "Accept": "text/html,text/plain,application/json,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(limit)
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, "ignore")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"[fetch failed: {e}]"


def _read_existing_skill(game_id: str) -> str:
    content = store.read_skill(game_id)
    return content[:20000]


def build_learn_prompt(game: dict, sources: list[str]) -> str:
    existing = _read_existing_skill(game["id"])
    visual_context = visual_memory.format_prompt_context(game["id"])
    fetched = []
    for url in sources[:8]:
        text = _fetch_url(url)
        fetched.append(f"## {url}\n{text[:12000]}")

    source_block = "\n\n".join(fetched) if fetched else "（使用者未提供來源；請自行用網路搜尋官方網站、wiki、攻略、入門教學與遊戲系統介紹。）"
    existing_block = existing or "（尚無既有 skill）"

    return f"""你正在為 AutoGameTest 建立一份遊戲操作 Skill。

請研究《{game.get('name', '')}》的網路資料，並輸出完整的 SKILL.md 內容。

遊戲設定：
```json
{json.dumps(game, ensure_ascii=False, indent=2)}
```

使用者提供或系統預先抓取的資料：
{source_block}

既有 Skill（若有，請保留有價值的內容並更新）：
{existing_block}

既有圖片記憶（若有，請整理進 SKILL.md 的「圖片記憶」章節）：
{visual_context}

要求：
- 若資料不足，請主動查找公開網路資料；優先官方網站、官方公告、wiki、可靠攻略。
- 不要輸出解釋，不要包 markdown code fence，只輸出 SKILL.md 本文。
- 使用繁體中文。
- 內容必須包含以下章節：
  # <遊戲名稱>
  ## 遊戲概述
  ## 啟動流程
  ## UI 地圖
  ## 例行任務
  ## 風險守則
  ## 圖片記憶
  ## 操作流程
  ## 經驗教訓
- 風險守則必須包含：不代輸帳密、不處理第三方登入授權、不代為消費、不自動打線上排位、每一步操作後截圖驗證。
- 圖片記憶章節請描述已知畫面、截圖路徑、signature 摘要、可點區域、風險標籤；不要把大型圖片或 base64 直接寫進 SKILL.md。
- 若是 Android/emulator 控制，請納入 ADB 截圖與 tap 的操作注意事項；若是 desktop 控制，請納入需 computer-use 的限制。
"""


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


def run_learn(game_id: str, sources: list[str] | None = None, job_id: str | None = None,
              engine: str = "codex", fallback: bool = False, timeout: int = 1200) -> dict:
    game = store.get_game(game_id)
    if not game:
        return {"ok": False, "error": f"遊戲不存在: {game_id}"}

    sources = sources or game.get("learn_sources", []) or []
    prompt = build_learn_prompt(game, sources)

    if job_id:
        store.update_job(job_id, status="running")

    try:
        result = ai_runner.run_with_fallback(
            prompt,
            cwd=ROOT,
            timeout=timeout,
            engine=engine,
            fallback=fallback,
            codex_sandbox="danger-full-access",
        )
        content = (result.get("output") or "").strip()
        if result.get("ok") and content:
            store.write_skill(game_id, content.rstrip() + "\n")
            status = "done"
            message = f"[engine={result.get('engine_used')}] 已建立/更新 Skill：{game.get('skill_path')}"
        else:
            status = "error"
            message = f"[engine={result.get('engine_used')}] {result.get('reason', 'learn failed')}"
    except Exception as e:
        result = {
            "ok": False,
            "engine_used": "none",
            "reason": f"runner crashed: {e}",
            "attempts": [],
            "traceback": traceback.format_exc(),
        }
        status = "error"
        message = result["reason"]

    if job_id:
        store.update_job(
            job_id,
            status=status,
            engine_used=result.get("engine_used"),
            run_reason=result.get("reason", ""),
            attempts=_summarize_attempts(result.get("attempts", [])),
            result=message,
            error_trace=(result.get("traceback") or "")[:4000] or None,
        )
    return {**result, "result": message}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Learn a game and write its SKILL.md")
    ap.add_argument("--game", help="game id")
    ap.add_argument("--sources", nargs="*", default=None)
    ap.add_argument("--job", help="process queued learn job")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args(argv)

    game_id = args.game
    sources = args.sources
    if args.job:
        job = store.get_job(args.job)
        if not job:
            print(f"job 不存在: {args.job}", file=sys.stderr)
            return 2
        payload = job.get("payload", {})
        game_id = game_id or payload.get("game_id")
        sources = sources if sources is not None else payload.get("sources", [])

    if not game_id:
        print("缺少 --game 或 --job", file=sys.stderr)
        return 2

    result = run_learn(
        game_id,
        sources=sources,
        job_id=args.job,
        engine=args.engine,
        fallback=False,
        timeout=args.timeout,
    )
    print(result.get("result") or result.get("output") or result.get("error", ""))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
