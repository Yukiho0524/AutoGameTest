"""Manage visual memory screenshots for AutoGameTest."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import visual_memory  # noqa: E402


def _parse_json(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Visual memory helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="remember a screenshot")
    add.add_argument("game_id")
    add.add_argument("image")
    add.add_argument("--label", default="")
    add.add_argument("--state", default="")
    add.add_argument("--note", default="")
    add.add_argument("--tags", default="")
    add.add_argument("--risk", default="safe")
    add.add_argument("--regions-json", default="", help="JSON list of UI regions")
    add.add_argument("--actions-json", default="", help="JSON list of safe action hints")
    add.add_argument("--no-copy", action="store_true", help="store original path only")

    ls = sub.add_parser("list", help="list visual memory")
    ls.add_argument("game_id")
    ls.add_argument("--limit", type=int, default=20)

    ctx = sub.add_parser("context", help="print prompt context for a game")
    ctx.add_argument("game_id")
    ctx.add_argument("--limit", type=int, default=20)

    args = ap.parse_args(argv)
    if args.cmd == "add":
        result = visual_memory.remember_image(
            args.game_id,
            args.image,
            label=args.label,
            state=args.state,
            note=args.note,
            tags=args.tags,
            risk=args.risk,
            regions=_parse_json(args.regions_json, []),
            actions=_parse_json(args.actions_json, []),
            source="cli",
            copy_image=not args.no_copy,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "list":
        print(visual_memory.summary(args.game_id, args.limit))
        return 0
    if args.cmd == "context":
        print(visual_memory.format_prompt_context(args.game_id, args.limit))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
