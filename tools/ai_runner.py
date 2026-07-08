"""Codex CLI runner for AutoGameTest scripted AI tasks.

Usage
-----
    python tools/ai_runner.py "your prompt here"
    python tools/ai_runner.py --cwd C:/path --timeout 300 "prompt"
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


def run_codex(prompt: str, cwd: str | None, timeout: int, sandbox: str) -> dict:
    exe = find_codex()
    if not exe:
        return {"engine": "codex", "ok": False, "found": False,
                "detail": "codex.exe not found"}
    last_file = os.path.join(tempfile.gettempdir(), "_agt_codex_last.txt")
    try:
        if os.path.exists(last_file):
            os.remove(last_file)
    except OSError:
        pass
    args = [exe, "exec", "-s", sandbox, "--skip-git-repo-check",
            "-o", last_file]
    if cwd:
        args += ["-C", cwd]
    args.append("-")
    try:
        proc = subprocess.run(
            args, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="ignore",
            cwd=cwd, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"engine": "codex", "ok": False, "found": True,
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
            "output": out, "detail": _clip((proc.stderr or "").strip(), 2000)}


def run_with_fallback(prompt: str, cwd: str | None = None, timeout: int = 600,
                      engine: str = "codex", fallback: bool = False,
                      codex_sandbox: str = "workspace-write") -> dict:
    """Compatibility wrapper: AutoGameTest now always runs Codex only."""
    _ = fallback
    if engine not in ("auto", "codex"):
        return {"engine_used": "none", "ok": False, "output": "",
                "attempts": [], "reason": f"unsupported engine: {engine}"}
    r = run_codex(prompt, cwd, timeout, codex_sandbox)
    return {"engine_used": "codex", "ok": r["ok"],
            "output": r.get("output", ""), "attempts": [r],
            "reason": "" if r["ok"] else r.get("detail", "codex failed")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Codex AI runner")
    ap.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
    ap.add_argument("--cwd", default=None, help="working directory for the agent")
    ap.add_argument("--timeout", type=int, default=600, help="timeout (s)")
    ap.add_argument("--engine", choices=["auto", "codex"], default="codex")
    ap.add_argument("--codex-sandbox", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"])
    args = ap.parse_args(argv)

    prompt = args.prompt
    if prompt in (None, "-"):
        prompt = sys.stdin.read()
    if not prompt or not prompt.strip():
        ap.error("empty prompt")

    result = run_with_fallback(
        prompt, cwd=args.cwd, timeout=args.timeout, engine=args.engine,
        codex_sandbox=args.codex_sandbox)

    print(result.get("output", ""))
    summary = {k: result[k] for k in ("engine_used", "ok", "reason") if k in result}
    summary["attempts"] = [
        {"engine": a["engine"], "ok": a.get("ok"),
         "found": a.get("found"), "detail": a.get("detail", "")[:120]}
        for a in result.get("attempts", [])
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
