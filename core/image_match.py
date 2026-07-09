"""Small PNG template matcher for script replay.

The project intentionally avoids requiring OpenCV on every user's machine, so
this module uses the existing stdlib PNG decoder from fast_agent and a sampled
mean-absolute-difference matcher. It is not meant for fuzzy object recognition;
it is for stable UI buttons cropped from recordings.
"""
from __future__ import annotations

import math
import os
from typing import Any

from . import fast_agent


DEFAULT_THRESHOLD = 0.88


def normalize_region(region: Any, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a normalized/pixel region to clamped pixel bounds."""
    if region is None:
        return 0, 0, width, height
    if isinstance(region, dict):
        vals = [region.get(k) for k in ("x1", "y1", "x2", "y2")]
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        vals = list(region)
    else:
        return 0, 0, width, height
    try:
        nums = [float(v) for v in vals]
    except (TypeError, ValueError):
        return 0, 0, width, height
    if all(0.0 <= v <= 1.0 for v in nums):
        x1, y1 = int(nums[0] * width), int(nums[1] * height)
        x2, y2 = int(nums[2] * width), int(nums[3] * height)
    else:
        x1, y1, x2, y2 = [int(round(v)) for v in nums]
    left, right = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
    top, bottom = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
    if right <= left or bottom <= top:
        return 0, 0, width, height
    return left, top, right, bottom


def match_template(screen_png: bytes, template_path: str, *,
                   threshold: float = DEFAULT_THRESHOLD,
                   region: Any = None,
                   scan_step: int | None = None,
                   max_points: int = 121) -> dict:
    """Find template_path in screen_png.

    Returns a dict with found/score and normalized center x/y. The matcher uses
    sampled pixels for speed, which is good enough for stable emulator UI crops.
    """
    if not template_path or not os.path.isfile(template_path):
        return {"found": False, "score": 0.0,
                "error": f"template not found: {template_path}"}
    with open(template_path, "rb") as f:
        template_png = f.read()
    try:
        sw, sh, screen = fast_agent._decode_png_rgb(screen_png)
        tw, th, template = fast_agent._decode_png_rgb(template_png)
    except Exception as e:
        return {"found": False, "score": 0.0, "error": str(e)}
    if tw <= 0 or th <= 0 or tw > sw or th > sh:
        return {"found": False, "score": 0.0,
                "error": f"template size {tw}x{th} incompatible with screen {sw}x{sh}"}

    x1, y1, x2, y2 = normalize_region(region, sw, sh)
    x2 = min(x2, sw - tw + 1)
    y2 = min(y2, sh - th + 1)
    if x2 <= x1 or y2 <= y1:
        return {"found": False, "score": 0.0, "error": "search region too small"}

    points = _sample_points(tw, th, max_points=max_points)
    step = scan_step or max(3, min(18, min(tw, th) // 8 or 3))
    best_score = -1.0
    best_x = x1
    best_y = y1
    for yy in range(y1, y2, step):
        for xx in range(x1, x2, step):
            score = _score_at(screen, template, xx, yy, points)
            if score > best_score:
                best_score = score
                best_x, best_y = xx, yy

    # Refine around the coarse result with a one-pixel local search.
    refine_radius = max(1, step)
    rx1 = max(x1, best_x - refine_radius)
    ry1 = max(y1, best_y - refine_radius)
    rx2 = min(x2, best_x + refine_radius + 1)
    ry2 = min(y2, best_y + refine_radius + 1)
    for yy in range(ry1, ry2):
        for xx in range(rx1, rx2):
            score = _score_at(screen, template, xx, yy, points)
            if score > best_score:
                best_score = score
                best_x, best_y = xx, yy

    cx = best_x + tw / 2.0
    cy = best_y + th / 2.0
    score = max(0.0, min(1.0, best_score))
    return {
        "found": score >= float(threshold),
        "score": round(score, 4),
        "threshold": float(threshold),
        "x": cx / max(1, sw - 1),
        "y": cy / max(1, sh - 1),
        "px": int(round(cx)),
        "py": int(round(cy)),
        "template": template_path,
        "template_width": tw,
        "template_height": th,
        "screen_width": sw,
        "screen_height": sh,
    }


def _sample_points(width: int, height: int,
                   max_points: int = 121) -> list[tuple[int, int]]:
    side = max(3, int(math.sqrt(max_points)))
    xs = sorted({min(width - 1, round((i + 0.5) * width / side))
                 for i in range(side)})
    ys = sorted({min(height - 1, round((i + 0.5) * height / side))
                 for i in range(side)})
    return [(x, y) for y in ys for x in xs]


def _score_at(screen, template, sx: int, sy: int,
              points: list[tuple[int, int]]) -> float:
    total = 0
    for tx, ty in points:
        sr, sg, sb = screen[sy + ty][sx + tx]
        tr, tg, tb = template[ty][tx]
        total += abs(sr - tr) + abs(sg - tg) + abs(sb - tb)
    max_diff = len(points) * 255 * 3
    return 1.0 - (total / max_diff if max_diff else 1.0)
