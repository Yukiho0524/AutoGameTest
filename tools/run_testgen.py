"""Run planning-document to QA TestCase generation jobs."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import store, testcases  # noqa: E402
import ai_runner  # noqa: E402


def _settings() -> tuple[int, str, str]:
    settings = store.get_settings()
    timeout = int(settings.get("ai_timeout_seconds", 3600) or 3600)
    model = str(settings.get("codex_model", "gpt-5.5") or "gpt-5.5")
    effort = str(settings.get("codex_reasoning_effort", "high") or "high")
    return timeout, model, effort


def run(doc_path: str, job_id: str | None = None,
        doc_name: str | None = None,
        game_id: str | None = None,
        mode: str = "standard",
        testcase_name: str | None = None,
        timeout: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None) -> dict:
    default_timeout, default_model, default_effort = _settings()
    timeout = int(timeout or default_timeout)
    model = model or default_model
    reasoning_effort = reasoning_effort or default_effort
    started = time.perf_counter()

    def progress(message: str) -> None:
        if job_id:
            store.update_job(
                job_id,
                progress={"stage": "testgen", "message": message},
            )

    def run_ai(prompt: str) -> dict:
        return ai_runner.run_with_fallback(
            prompt,
            cwd=ROOT,
            timeout=timeout,
            engine="codex",
            codex_sandbox="workspace-write",
            codex_model=model,
            codex_reasoning_effort=reasoning_effort,
        )

    game = store.get_game(game_id) if game_id else None
    if mode == "destructive":
        result = testcases.generate_destructive_testcases(
            testcase_name or doc_name or doc_path,
            run_ai=run_ai,
            on_progress=progress,
            autopush=True,
        )
    else:
        result = testcases.generate_testcases(
            doc_path,
            run_ai=run_ai,
            on_progress=progress,
            doc_name=doc_name,
            game=game,
            autopush=True,
        )
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    result["model"] = model
    result["reasoning_effort"] = reasoning_effort
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate QA TestCase from planning document")
    parser.add_argument("doc", nargs="?", help="planning document path")
    parser.add_argument("--job", default="")
    parser.add_argument("--engine", default="codex")
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--destructive-from", default="")
    args = parser.parse_args(argv)

    job_id = args.job.strip()
    doc_path = args.doc
    doc_name = None
    game_id = None
    mode = "destructive" if args.destructive_from else "standard"
    testcase_name = args.destructive_from or None
    if job_id:
        job = store.get_job(job_id)
        if not job:
            print(f"job 不存在: {job_id}", file=sys.stderr)
            return 2
        payload = job.get("payload") or {}
        doc_path = payload.get("doc_path")
        doc_name = payload.get("filename")
        game_id = payload.get("game_id")
        mode = payload.get("mode", mode) or mode
        testcase_name = payload.get("testcase_name") or testcase_name
        store.update_job(job_id, status="running")
    if mode == "destructive" and not testcase_name:
        print("缺少來源 TestCase", file=sys.stderr)
        return 2
    if mode != "destructive" and not doc_path:
        print("缺少企劃書路徑", file=sys.stderr)
        return 2

    try:
        result = run(
            str(doc_path or ""),
            job_id=job_id or None,
            doc_name=doc_name,
            game_id=game_id,
            mode=mode,
            testcase_name=testcase_name,
            timeout=args.timeout,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
    except Exception as e:
        result = {
            "ok": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
        }

    if job_id:
        store.update_job(
            job_id,
            status="done" if result.get("ok") else "error",
            result=result.get("message") or result.get("error") or "",
            testcase=result,
            attempts=result.get("attempts", []),
            progress=None,
            error_trace=(result.get("traceback") or result.get("error") or "")[:4000],
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
