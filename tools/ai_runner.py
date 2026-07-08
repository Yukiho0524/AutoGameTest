"""AI runner with automatic Claude Code -> Codex CLI fallback.

Why this exists
---------------
When Claude Code's usage quota is exhausted, Claude Code itself is the thing that
can no longer run -- so a fallback CANNOT live *inside* a Claude Code session
reacting to its own death. It must be an EXTERNAL orchestrator: this script runs
a prompt through Claude first, detects a quota/usage-limit failure, and re-runs
the same prompt through Codex.

This works for headless/scripted prompts (job runner, batch tasks). It does NOT
transparently rescue a live interactive Claude Code chat -- that session's
context and MCP tools (computer-use, preview, ...) don't transfer to Codex.

Usage
-----
    python tools/ai_runner.py "your prompt here"
    python tools/ai_runner.py --cwd C:/path --timeout 300 "prompt"
    python tools/ai_runner.py --engine codex "prompt"     # force one engine
    python tools/ai_runner.py --no-fallback "prompt"       # Claude only
    echo "prompt from stdin" | python tools/ai_runner.py -

Exit code 0 if either engine produced a result, non-zero otherwise.
Prints a JSON summary to stderr; the model's text answer goes to stdout.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import config  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # answers may be Chinese; avoid cp950 crash
    except Exception:
        pass

_CREATE_NO_WINDOW = 0x08000000

# Signals that Claude failed specifically because the quota/credits are gone.
# Matched case-insensitively against combined stdout+stderr. Kept broad on
# purpose: a false positive only means we also try Codex, which is harmless.
QUOTA_PATTERNS = [
    r"usage limit",
    r"session limit",
    r"hit (?:your|the)? .*limit",
    r"rate limit",
    r"quota",
    r"token(?:s)? (?:limit|quota|budget|exhausted)",
    r"context (?:limit|length)",
    r"credit balance is too low",
    r"insufficient (?:credit|quota|balance)",
    r"out of (?:credit|quota)",
    r"limit reached",
    r"resets? at",
    r"429",
    r"too many requests",
    r"upgrade to (?:a )?paid",
    r"billing",
    r"payment required",
    r"額度",
    r"不足",
    r"用量上限",
    r"使用上限",
    r"配額",
    r"token不足",
    r"token 不足",
]


def _clip(text: str | None, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def find_claude() -> str | None:
    """Newest claude.exe under the Claude Code install dir."""
    configured = config.get("claude_path", "AUTOGAMETEST_CLAUDE_PATH")
    if configured and os.path.isfile(configured):
        return configured
    roots = [
        os.path.expandvars(r"%APPDATA%\Claude\claude-code"),
        os.path.expandvars(r"%LOCALAPPDATA%\AnthropicClaude"),
    ]
    cands = []
    for r in roots:
        cands += glob.glob(os.path.join(r, "*", "claude.exe"))
        cands += glob.glob(os.path.join(r, "claude.exe"))
    if not cands:
        from shutil import which
        w = which("claude")
        return w
    # sort by version-ish folder name, newest last
    cands.sort(key=lambda p: _ver_key(os.path.basename(os.path.dirname(p))))
    return cands[-1]


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
        # fall back to the CLI bundled with the desktop app
        cands += glob.glob(os.path.expandvars(
            r"%ProgramFiles%\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe"))
    if not cands:
        from shutil import which
        return which("codex")
    cands.sort(key=lambda p: os.path.getmtime(p))
    return cands[-1]


def _ver_key(name: str):
    parts = re.findall(r"\d+", name)
    return [int(x) for x in parts] if parts else [0]


def _looks_like_quota(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in QUOTA_PATTERNS)


def run_claude(prompt: str, cwd: str | None, timeout: int) -> dict:
    exe = find_claude()
    if not exe:
        return {"engine": "claude", "ok": False, "found": False,
                "detail": "claude.exe not found"}
    try:
        proc = subprocess.run(
            [exe, "-p", "--output-format", "text"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="ignore",
            cwd=cwd, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"engine": "claude", "ok": False, "found": True,
                "quota": False, "detail": f"timeout after {timeout}s"}
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    ok = proc.returncode == 0 and bool(proc.stdout.strip())
    quota = (not ok) and _looks_like_quota(combined)
    # strip the benign stdin warning Claude prints when nothing is piped
    out = re.sub(r"^Warning: no stdin data received.*?\n?", "", proc.stdout or "").strip()
    detail = (proc.stderr or "").strip()
    if not detail and not ok:
        detail = combined.strip()
    return {"engine": "claude", "ok": ok, "found": True, "quota": quota,
            "rc": proc.returncode, "output": out,
            "detail": _clip(detail, 2000)}


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
                      engine: str = "auto", fallback: bool = True,
                      codex_sandbox: str = "workspace-write") -> dict:
    """Try Claude, fall back to Codex on quota exhaustion (or Claude missing).

    engine: "auto" | "claude" | "codex"
    Returns {engine_used, ok, output, attempts:[...]}.
    """
    attempts = []

    if engine == "codex":
        r = run_codex(prompt, cwd, timeout, codex_sandbox)
        attempts.append(r)
        return {"engine_used": "codex", "ok": r["ok"],
                "output": r.get("output", ""), "attempts": attempts,
                "reason": "" if r["ok"] else r.get("detail", "codex failed")}

    # engine auto or claude: try Claude first
    rc = run_claude(prompt, cwd, timeout)
    attempts.append(rc)
    if rc["ok"]:
        return {"engine_used": "claude", "ok": True,
                "output": rc["output"], "attempts": attempts,
                "reason": "claude succeeded"}

    should_fallback = fallback and engine != "claude" and (
        rc.get("quota") or not rc.get("found"))
    if not should_fallback:
        return {"engine_used": "claude", "ok": False,
                "output": rc.get("output", ""), "attempts": attempts,
                "reason": "claude failed; no fallback ("
                          + ("quota not detected" if fallback else "disabled") + "): "
                          + rc.get("detail", "")[:500]}

    rx = run_codex(prompt, cwd, timeout, codex_sandbox)
    attempts.append(rx)
    reason = ("claude quota/token limit detected -> codex fallback"
              if rc.get("quota") else "claude missing -> codex fallback")
    if not rx["ok"]:
        reason += "; codex failed: " + rx.get("detail", "")[:500]
    return {"engine_used": "codex" if rx["ok"] else "none", "ok": rx["ok"],
            "output": rx.get("output", ""), "attempts": attempts,
            "reason": reason}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Claude->Codex fallback AI runner")
    ap.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
    ap.add_argument("--cwd", default=None, help="working directory for the agent")
    ap.add_argument("--timeout", type=int, default=600, help="per-engine timeout (s)")
    ap.add_argument("--engine", choices=["auto", "claude", "codex"], default="auto")
    ap.add_argument("--no-fallback", action="store_true", help="Claude only, no Codex")
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
        fallback=not args.no_fallback, codex_sandbox=args.codex_sandbox)

    # human/machine split: answer on stdout, summary on stderr
    print(result.get("output", ""))
    summary = {k: result[k] for k in ("engine_used", "ok", "reason") if k in result}
    summary["attempts"] = [
        {"engine": a["engine"], "ok": a.get("ok"), "quota": a.get("quota"),
         "found": a.get("found"), "detail": a.get("detail", "")[:120]}
        for a in result.get("attempts", [])
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
