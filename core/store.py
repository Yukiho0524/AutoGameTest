"""JSON-backed store for games, agents, skills, jobs, and schedules."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date

from core import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "data", "games.json")
JOBS_DIR = os.path.join(ROOT, "data", "jobs")
SCHEDULES_FILE = os.path.join(ROOT, "data", "schedules.json")
SETTINGS_FILE = os.path.join(ROOT, "data", "settings.json")
SKILLS_DIR = os.path.join(ROOT, ".codex", "skills")
AGENTS_DIR = os.path.join(ROOT, ".codex", "agents")
DEFAULT_SETTINGS = {
    "ai_timeout_seconds": 3600,
    "codex_model": "gpt-5.5",
    "codex_reasoning_effort": "high",
    "recording_dir": "",
}
CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.isfile(DATA_FILE):
        return {"games": [], "agents": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("games", [])
    data.setdefault("agents", [])
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "game"


# ---------------- games ----------------

def list_games() -> list[dict]:
    return _load()["games"]


def get_game(game_id: str) -> dict | None:
    return next((g for g in _load()["games"] if g["id"] == game_id), None)


def upsert_game(game: dict) -> dict:
    with _lock:
        data = _load()
        gid = game.get("id") or _slugify(game.get("name", ""))
        existing_ids = {g["id"] for g in data["games"]}
        if not game.get("id"):
            base, n = gid, 2
            while gid in existing_ids:
                gid = f"{base}-{n}"; n += 1
        game["id"] = gid
        game.setdefault("created", date.today().isoformat())
        game.setdefault("skill_path", f".codex/skills/{gid}/SKILL.md")
        game.setdefault("agent_path", f".codex/agents/{gid}-player.md")
        game.setdefault("verified", False)

        for i, g in enumerate(data["games"]):
            if g["id"] == gid:
                data["games"][i] = {**g, **game}
                break
        else:
            data["games"].append(game)
        _save(data)
        return game


def delete_game(game_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["games"])
        data["games"] = [g for g in data["games"] if g["id"] != game_id]
        data["agents"] = [a for a in data["agents"] if a.get("game_id") != game_id]
        _save(data)
        return len(data["games"]) < before


# ---------------- agents ----------------

def list_agents(game_id: str | None = None) -> list[dict]:
    agents = _load()["agents"]
    if game_id:
        return [a for a in agents if a.get("game_id") == game_id]
    return agents


def upsert_agent(agent: dict) -> dict:
    with _lock:
        data = _load()
        aid = agent.get("id") or (_slugify(agent.get("game_id", "")) + "-" +
                                   _slugify(agent.get("name", "agent")))
        agent["id"] = aid
        agent.setdefault("created", date.today().isoformat())
        for i, a in enumerate(data["agents"]):
            if a["id"] == aid:
                data["agents"][i] = {**a, **agent}
                break
        else:
            data["agents"].append(agent)
        _save(data)
        return agent


def delete_agent(agent_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["agents"])
        data["agents"] = [a for a in data["agents"] if a["id"] != agent_id]
        _save(data)
        return len(data["agents"]) < before


# ---------------- skill files ----------------

def read_skill(game_id: str) -> str:
    g = get_game(game_id)
    if not g:
        return ""
    path = os.path.join(ROOT, g.get("skill_path", ""))
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def write_skill(game_id: str, content: str) -> None:
    g = get_game(game_id)
    if not g:
        return
    path = os.path.join(ROOT, g.get("skill_path", f".codex/skills/{game_id}/SKILL.md"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def append_skill_lessons(game_id: str, lessons: list[str],
                         source: str = "run_agent") -> dict:
    g = get_game(game_id)
    if not g:
        return {"appended": 0, "skipped": len(lessons), "path": "", "error": "game not found"}

    clean = []
    for lesson in lessons[:12]:
        text = re.sub(r"\s+", " ", str(lesson or "")).strip(" -\t\r\n")
        if text and text not in clean:
            clean.append(text[:700])
    if not clean:
        return {"appended": 0, "skipped": len(lessons), "path": "", "error": ""}

    path = os.path.join(ROOT, g.get("skill_path", f".codex/skills/{game_id}/SKILL.md"))
    content = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    if not content.strip():
        content = f"# {g.get('name') or game_id}\n\n## 經驗教訓\n"

    date_tag = date.today().isoformat()
    lines = []
    skipped = 0
    for text in clean:
        if text in content:
            skipped += 1
            continue
        lines.append(f"- {date_tag} [{source}] {text}")
    if not lines:
        return {"appended": 0, "skipped": skipped, "path": path, "error": ""}

    block = "\n".join(lines) + "\n"
    match = re.search(r"(?m)^(#{1,6})\s*經驗教訓\s*$", content)
    if match:
        level = len(match.group(1))
        next_heading = re.search(rf"(?m)^#{{1,{level}}}\s+", content[match.end():])
        insert_at = len(content) if not next_heading else match.end() + next_heading.start()
        before = content[:insert_at].rstrip()
        after = content[insert_at:].lstrip("\n")
        content = before + "\n" + block
        if after:
            content += "\n" + after
    else:
        content = content.rstrip() + "\n\n## 經驗教訓\n" + block

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content if content.endswith("\n") else content + "\n")
    return {"appended": len(lines), "skipped": skipped, "path": path, "error": ""}


# ---------------- jobs ----------------
# Learning a game and running an agent require AI cognition. The UI enqueues a
# job file, then a Codex-backed runner processes it and marks it done.

def enqueue_job(kind: str, payload: dict) -> dict:
    os.makedirs(JOBS_DIR, exist_ok=True)
    import time, uuid
    job = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,          # "learn" | "run_agent"
        "status": "pending",   # pending | running | done | error
        "payload": payload,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "result": None,
    }
    with open(os.path.join(JOBS_DIR, f"{job['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    return job


def list_jobs() -> list[dict]:
    if not os.path.isdir(JOBS_DIR):
        return []
    jobs = []
    for fn in os.listdir(JOBS_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(JOBS_DIR, fn), "r", encoding="utf-8") as f:
                    jobs.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(jobs, key=lambda j: j.get("created", ""), reverse=True)


def delete_job(job_id: str) -> bool:
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def clear_jobs(scope: str = "finished") -> int:
    """Delete jobs. scope: 'finished' (done+error) or 'all'. Returns count removed.

    'running' and 'pending' jobs are kept when scope='finished' so an in-flight
    agent run isn't orphaned from its status file mid-execution.
    """
    removed = 0
    for j in list_jobs():
        if scope == "all" or j.get("status") in ("done", "error"):
            if delete_job(j["id"]):
                removed += 1
    return removed


def get_job(job_id: str) -> dict | None:
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def update_job(job_id: str, **fields) -> dict | None:
    with _lock:
        path = os.path.join(JOBS_DIR, f"{job_id}.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            job = json.load(f)
        job.update(fields)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return job


def get_agent(agent_id: str) -> dict | None:
    return next((a for a in _load()["agents"] if a["id"] == agent_id), None)


# ---------------- settings ----------------

def _clean_settings(settings: dict | None) -> dict:
    raw = settings if isinstance(settings, dict) else {}
    clean = dict(DEFAULT_SETTINGS)
    clean.update(raw)
    try:
        timeout = int(clean.get("ai_timeout_seconds", DEFAULT_SETTINGS["ai_timeout_seconds"]))
    except (TypeError, ValueError):
        timeout = DEFAULT_SETTINGS["ai_timeout_seconds"]
    clean["ai_timeout_seconds"] = max(60, min(86400, timeout))
    configured_model = config.get(
        "codex_model",
        "AUTOGAMETEST_CODEX_MODEL",
        "",
    )
    clean["codex_model"] = (
        str(raw.get("codex_model") or configured_model or DEFAULT_SETTINGS["codex_model"])
        .strip()
        or DEFAULT_SETTINGS["codex_model"]
    )
    configured_effort = config.get(
        "codex_reasoning_effort",
        "AUTOGAMETEST_CODEX_REASONING_EFFORT",
        "",
    )
    effort = str(
        raw.get(
            "codex_reasoning_effort",
        ) or configured_effort or DEFAULT_SETTINGS["codex_reasoning_effort"]
    ).strip().lower()
    clean["codex_reasoning_effort"] = (
        effort if effort in CODEX_REASONING_EFFORTS
        else DEFAULT_SETTINGS["codex_reasoning_effort"]
    )
    clean["recording_dir"] = str(clean.get("recording_dir", "") or "").strip()
    return clean


def get_settings() -> dict:
    if not os.path.isfile(SETTINGS_FILE):
        return _clean_settings({})
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _clean_settings({})
    return _clean_settings(data)


def save_settings(settings: dict) -> dict:
    clean = _clean_settings(settings)
    with _lock:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    return clean


# ---------------- schedules ----------------

def list_schedules() -> list[dict]:
    if not os.path.isfile(SCHEDULES_FILE):
        return []
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    schedules = data.get("schedules", data if isinstance(data, list) else [])
    return schedules if isinstance(schedules, list) else []


def save_schedules(schedules: list[dict]) -> list[dict]:
    clean = []
    for s in schedules:
        try:
            day = int(s.get("day"))
            hour = int(s.get("hour"))
            minute = int(s.get("minute", 0))
        except (TypeError, ValueError):
            continue
        if not (0 <= day <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        agent_id = str(s.get("agent_id", "")).strip()
        if not agent_id:
            continue
        sid = str(s.get("id") or f"{agent_id}-{day}-{hour}-{minute}")
        clean.append({
            "id": sid,
            "agent_id": agent_id,
            "day": day,
            "hour": hour,
            "minute": minute,
            "enabled": bool(s.get("enabled", True)),
            "last_run_key": str(s.get("last_run_key", "") or ""),
        })
    with _lock:
        os.makedirs(os.path.dirname(SCHEDULES_FILE), exist_ok=True)
        tmp = SCHEDULES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schedules": clean}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SCHEDULES_FILE)
    return clean


def mark_schedule_run(schedule_id: str, run_key: str) -> None:
    schedules = list_schedules()
    changed = False
    for s in schedules:
        if s.get("id") == schedule_id:
            s["last_run_key"] = run_key
            changed = True
            break
    if changed:
        save_schedules(schedules)
