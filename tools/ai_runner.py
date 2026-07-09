"""Codex CLI runner for AutoGameTest scripted AI tasks.

Usage
-----
    python tools/ai_runner.py "your prompt here"
    python tools/ai_runner.py --cwd C:/path --timeout 3600 "prompt"
    echo "prompt from stdin" | python tools/ai_runner.py -

Exit code 0 if Codex produced a result, non-zero otherwise.
Prints a JSON summary to stderr; the model's text answer goes to stdout.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import config  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_CREATE_NO_WINDOW = 0x08000000
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "high"
CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


def _clip(text: str | None, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def find_codex() -> str | None:
    """Newest codex.exe (standalone CLI preferred)."""
    configured = config.get("codex_path", "AUTOGAMETEST_CODEX_PATH")
    if configured and os.path.isfile(configured):
        return configured
    roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin"),
        os.path.expanduser(r"~\.codex\bin"),
    ]
    cands = []
    for r in roots:
        cands += glob.glob(os.path.join(r, "*", "codex.exe"))
        cands += glob.glob(os.path.join(r, "codex.exe"))
    if not cands:
        cands += glob.glob(os.path.expandvars(
            r"%ProgramFiles%\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe"))
    if not cands:
        from shutil import which
        return which("codex")
    cands.sort(key=lambda p: os.path.getmtime(p))
    return cands[-1]


def _clean_model(value: str | None) -> str:
    return str(value or DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL


def _clean_reasoning_effort(value: str | None) -> str:
    effort = str(value or DEFAULT_CODEX_REASONING_EFFORT).strip().lower()
    return effort if effort in CODEX_REASONING_EFFORTS else DEFAULT_CODEX_REASONING_EFFORT


def configured_codex_model() -> str:
    return _clean_model(config.get(
        "codex_model",
        "AUTOGAMETEST_CODEX_MODEL",
        DEFAULT_CODEX_MODEL,
    ))


def configured_codex_reasoning_effort() -> str:
    return _clean_reasoning_effort(config.get(
        "codex_reasoning_effort",
        "AUTOGAMETEST_CODEX_REASONING_EFFORT",
        DEFAULT_CODEX_REASONING_EFFORT,
    ))


def build_codex_args(exe: str, sandbox: str, last_file: str,
                     cwd: str | None = None, model: str | None = None,
                     reasoning_effort: str | None = None) -> list[str]:
    model = _clean_model(model)
    reasoning_effort = _clean_reasoning_effort(reasoning_effort)
    args = [exe, "exec", "-s", sandbox, "--skip-git-repo-check",
            "--model", model,
            "-c", f'model_reasoning_effort="{reasoning_effort}"',
            "-o", last_file]
    if cwd:
        args += ["-C", cwd]
    args.append("-")
    return args


def run_codex(prompt: str, cwd: str | None, timeout: int, sandbox: str,
              model: str | None = None,
              reasoning_effort: str | None = None) -> dict:
    exe = find_codex()
    if not exe:
        return {"engine": "codex", "ok": False, "found": False,
                "detail": "codex.exe not found"}
    model = _clean_model(model or configured_codex_model())
    reasoning_effort = _clean_reasoning_effort(
        reasoning_effort or configured_codex_reasoning_effort())
    last_file = os.path.join(tempfile.gettempdir(), "_agt_codex_last.txt")
    try:
        if os.path.exists(last_file):
            os.remove(last_file)
    except OSError:
        pass
    args = build_codex_args(
        exe, sandbox, last_file, cwd=cwd,
        model=model, reasoning_effort=reasoning_effort)
    try:
        proc = subprocess.run(
            args, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="ignore",
            cwd=cwd, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"engine": "codex", "ok": False, "found": True,
                "model": model, "reasoning_effort": reasoning_effort,
                "detail": f"timeout after {timeout}s"}
    out = ""
    if os.path.exists(last_file):
        try:
            with open(last_file, "r", encoding="utf-8", errors="ignore") as f:
                out = f.read().strip()
        except OSError:
            pass
    if not out:
        out = (proc.stdout or "").strip()
    ok = proc.returncode == 0 and bool(out)
    return {"engine": "codex", "ok": ok, "found": True, "rc": proc.returncode,
            "model": model, "reasoning_effort": reasoning_effort,
            "output": out, "detail": _clip((proc.stderr or "").strip(), 2000)}


def run_with_fallback(prompt: str, cwd: str | None = None, timeout: int = 3600,
                      engine: str = "codex", fallback: bool = False,
                      codex_sandbox: str = "workspace-write",
                      codex_model: str | None = None,
                      codex_reasoning_effort: str | None = None) -> dict:
    """Compatibility wrapper: AutoGameTest now always runs Codex only."""
    _ = fallback
    if engine not in ("auto", "codex"):
        return {"engine_used": "none", "ok": False, "output": "",
                "attempts": [], "reason": f"unsupported engine: {engine}"}
    r = run_codex(
        prompt, cwd, timeout, codex_sandbox,
        model=codex_model, reasoning_effort=codex_reasoning_effort)
    return {"engine_used": "codex", "ok": r["ok"],
            "output": r.get("output", ""), "attempts": [r],
            "reason": "" if r["ok"] else r.get("detail", "codex failed")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex AI runner")
    ap.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
    ap.add_argument("--cwd", default=None, help="working directory for the agent")
    ap.add_argument("--timeout", type=int, default=3600, help="timeout (s)")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--codex-sandbox", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"])
    ap.add_argument("--model", default=None, help="Codex model, default gpt-5.5")
    ap.add_argument("--reasoning-effort", default=None,
                    choices=sorted(CODEX_REASONING_EFFORTS),
                    help="Codex reasoning effort, default high")
    args = ap.parse_args(argv)

    prompt = args.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        ap.error("empty prompt")

    result = run_with_fallback(
        prompt, cwd=args.cwd, timeout=args.timeout, engine=args.engine,
        codex_sandbox=args.codex_sandbox,
        codex_model=args.model,
        codex_reasoning_effort=args.reasoning_effort)

    print(result.get("output", ""))
    summary = {k: result[k] for k in ("engine_used", "ok", "reason") if k in result}
    summary["attempts"] = [
        {"engine": a["engine"], "ok": a.get("ok"),
         "found": a.get("found"), "model": a.get("model"),
         "reasoning_effort": a.get("reasoning_effort"),
         "detail": a.get("detail", "")[:120]}
        for a in result.get("attempts", [])
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
