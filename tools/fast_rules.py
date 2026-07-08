"""Inspect and manage AutoGameTest fast-agent rules."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import fast_agent  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fast rule helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sig = sub.add_parser("signature", help="print screenshot signature JSON")
    sig.add_argument("png", help="PNG screenshot path")

    ls = sub.add_parser("list", help="list fast rules for a game")
    ls.add_argument("game_id")
    ls.add_argument("--include-visual", action="store_true",
                    help="also show safe visual-memory rules used by the fast layer")

    args = ap.parse_args(argv)
    if args.cmd == "signature":
        print(json.dumps(fast_agent.signature_for_file(args.png),
                         ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "list":
        print(fast_agent.rules_summary(args.game_id))
        if args.include_visual:
            visual_rules = fast_agent.load_visual_memory_rules(args.game_id)
            print()
            print("Visual-memory fast rules:")
            if not visual_rules:
                print("目前沒有可進入快速層的圖片記憶。")
            for rule in visual_rules:
                print(
                    f"- {rule.get('id')}: {rule.get('description')} "
                    f"({len(rule.get('actions', []))} actions)")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
