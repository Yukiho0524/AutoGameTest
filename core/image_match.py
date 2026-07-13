"""Small PNG template matcher for script replay.

OpenCV is used when available for Airtest-like normalized cross-correlation.
The project still runs without OpenCV: the fallback uses the existing stdlib
PNG decoder from fast_agent and a sampled NCC matcher.
"""
from __future__ import annotations

import math
import os
from typing import Any

from . import fast_agent

try:  # OpenCV is optional on other users' machines.
    import cv2  # type: ignore
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - depends on local environment
    cv2 = None
    np = None


DEFAULT_THRESHOLD = 0.72
MIN_TEMPLATE_STDDEV = 4.0


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
                   record_pos: Any = None,
                   resolution: Any = None,
                   rgb: bool = False,
                   scan_step: int | None = None,
                   max_points: int = 121,
                   allow_full_search: bool | None = None) -> dict:
    """Find template_path in screen_png.

    Returns a dict with found/score and normalized center x/y. The matcher uses
    sampled pixels for speed, which is good enough for stable emulator UI crops.
    """
    if not template_path or not os.path.isfile(template_path):
        return {"found": False, "score": 0.0,
                "error": f"template not found: {template_path}"}
    with open(template_path, "rb") as f:
        template_png = f.read()
    if cv2 is not None and np is not None:
        result = _match_template_cv2(
            screen_png, template_png, template_path,
            threshold=threshold, region=region, record_pos=record_pos,
            resolution=resolution, rgb=rgb,
            allow_full_search=allow_full_search)
        if result is not None:
            return result
    try:
        sw, sh, screen = fast_agent._decode_png_rgb(screen_png)
        tw, th, template = fast_agent._decode_png_rgb(template_png)
    except Exception as e:
        return {"found": False, "score": 0.0, "error": str(e)}
    if tw <= 0 or th <= 0 or tw > sw or th > sh:
        return {"found": False, "score": 0.0,
                "error": f"template size {tw}x{th} incompatible with screen {sw}x{sh}"}

    points = _sample_points(tw, th, max_points=max_points)
    step = scan_step or _default_scan_step(record_pos, tw, th)
    search_regions = _search_regions(
        region, record_pos, resolution, sw, sh, tw, th, allow_full_search)

    best = None
    for mode, bounds in search_regions:
        result = _search_region(
            screen, template, bounds, sw, sh, tw, th, points, step, rgb=rgb)
        if not result:
            continue
        if mode == "predicted":
            focused = _search_expected_position(
                screen, template, record_pos, resolution, sw, sh, tw, th,
                points, rgb=rgb)
            if focused and focused["score"] > result["score"]:
                result = focused
        result["search_mode"] = mode
        if best is None or result["score"] > best["score"]:
            best = result
        if result["score"] >= float(threshold):
            return _format_result(result, threshold, template_path, tw, th, sw, sh)

    if best is None:
        return {"found": False, "score": 0.0, "threshold": float(threshold),
                "error": "search region too small", "template": template_path}
    return _format_result(best, threshold, template_path, tw, th, sw, sh)


def _match_template_cv2(screen_png: bytes, template_png: bytes,
                        template_path: str, *, threshold: float,
                        region: Any, record_pos: Any, resolution: Any,
                        rgb: bool,
                        allow_full_search: bool | None) -> dict | None:
    flag = cv2.IMREAD_COLOR if rgb else cv2.IMREAD_GRAYSCALE
    screen_img = cv2.imdecode(np.frombuffer(screen_png, np.uint8), flag)
    template_img = cv2.imdecode(np.frombuffer(template_png, np.uint8), flag)
    if screen_img is None or template_img is None:
        return None
    sh, sw = screen_img.shape[:2]
    th, tw = template_img.shape[:2]
    if tw <= 0 or th <= 0 or tw > sw or th > sh:
        return {"found": False, "score": 0.0,
                "error": f"template size {tw}x{th} incompatible with screen {sw}x{sh}"}

    gray_template = (cv2.cvtColor(template_img, cv2.COLOR_BGR2GRAY)
                     if rgb else template_img)
    _, stddev = cv2.meanStdDev(gray_template)
    if float(stddev[0][0]) < MIN_TEMPLATE_STDDEV:
        return {"found": False, "score": 0.0,
                "threshold": float(threshold),
                "error": "template too plain for reliable matching",
                "template": template_path}

    search_regions = _search_regions(
        region, record_pos, resolution, sw, sh, tw, th, allow_full_search)
    best = None
    for mode, bounds in search_regions:
        result = _search_region_cv2(screen_img, template_img, bounds,
                                    sw, sh, tw, th)
        if not result:
            continue
        result["search_mode"] = mode
        if best is None or result["score"] > best["score"]:
            best = result
        if result["score"] >= float(threshold):
            return _format_result(result, threshold, template_path,
                                  tw, th, sw, sh)

    if best is None:
        return {"found": False, "score": 0.0, "threshold": float(threshold),
                "error": "search region too small", "template": template_path}
    return _format_result(best, threshold, template_path, tw, th, sw, sh)


def _search_region_cv2(screen_img, template_img,
                       bounds: tuple[int, int, int, int],
                       sw: int, sh: int, tw: int, th: int) -> dict | None:
    x1, y1, x2, y2 = bounds
    x2 = min(x2, sw - tw + 1)
    y2 = min(y2, sh - th + 1)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = screen_img[y1:y2 + th - 1, x1:x2 + tw - 1]
    if crop.shape[0] < th or crop.shape[1] < tw:
        return None
    scores = cv2.matchTemplate(crop, template_img, cv2.TM_CCOEFF_NORMED)
    scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, max_val, _, max_loc = cv2.minMaxLoc(scores)
    best_x = x1 + int(max_loc[0])
    best_y = y1 + int(max_loc[1])
    return {"score": float(max_val),
            "left": best_x, "top": best_y,
            "right": best_x + tw, "bottom": best_y + th}


def _sample_points(width: int, height: int,
                   max_points: int = 121) -> list[tuple[int, int]]:
    side = max(3, int(math.sqrt(max_points)))
    xs = sorted({min(width - 1, round((i + 0.5) * width / side))
                 for i in range(side)})
    ys = sorted({min(height - 1, round((i + 0.5) * height / side))
                 for i in range(side)})
    return [(x, y) for y in ys for x in xs]


def _default_scan_step(record_pos: Any, tw: int, th: int) -> int:
    coarse = max(3, min(18, min(tw, th) // 8 or 3))
    if record_pos is not None:
        return min(coarse, 5)
    return coarse


def _search_regions(region: Any, record_pos: Any, resolution: Any,
                    sw: int, sh: int, tw: int, th: int,
                    allow_full_search: bool | None) -> list[tuple[str, tuple[int, int, int, int]]]:
    if region is not None:
        return [("region", normalize_region(region, sw, sh))]
    predicted = _predict_region(record_pos, resolution, sw, sh, tw, th)
    if predicted:
        regions = [("predicted", predicted)]
        if allow_full_search is True:
            regions.append(("full", normalize_region(None, sw, sh)))
        return regions
    return [("full", normalize_region(None, sw, sh))]


def _search_region(screen, template, bounds: tuple[int, int, int, int],
                   sw: int, sh: int, tw: int, th: int,
                   points: list[tuple[int, int]], step: int,
                   rgb: bool = False) -> dict | None:
    x1, y1, x2, y2 = bounds
    x2 = min(x2, sw - tw + 1)
    y2 = min(y2, sh - th + 1)
    if x2 <= x1 or y2 <= y1:
        return None
    best_score = -1.0
    best_x = x1
    best_y = y1
    for yy in range(y1, y2, step):
        for xx in range(x1, x2, step):
            score = _score_at(screen, template, xx, yy, points, rgb=rgb)
            if score > best_score:
                best_score = score
                best_x, best_y = xx, yy

    refine_radius = max(1, step)
    rx1 = max(x1, best_x - refine_radius)
    ry1 = max(y1, best_y - refine_radius)
    rx2 = min(x2, best_x + refine_radius + 1)
    ry2 = min(y2, best_y + refine_radius + 1)
    for yy in range(ry1, ry2):
        for xx in range(rx1, rx2):
            score = _score_at(screen, template, xx, yy, points, rgb=rgb)
            if score > best_score:
                best_score = score
                best_x, best_y = xx, yy
    return {"score": max(0.0, min(1.0, best_score)),
            "left": best_x, "top": best_y,
            "right": best_x + tw, "bottom": best_y + th}


def _search_expected_position(screen, template, record_pos: Any,
                              resolution: Any, sw: int, sh: int,
                              tw: int, th: int,
                              points: list[tuple[int, int]],
                              rgb: bool = False) -> dict | None:
    expected = _expected_top_left(record_pos, resolution, sw, sh, tw, th)
    if expected is None:
        return None
    ex, ey = expected
    radius = max(8, min(24, max(tw, th) // 10))
    bounds = (ex - radius, ey - radius, ex + radius + 1, ey + radius + 1)
    bounds = normalize_region(bounds, sw, sh)
    return _search_region(screen, template, bounds, sw, sh, tw, th,
                          points, 1, rgb=rgb)


def _format_result(result: dict, threshold: float, template_path: str,
                   tw: int, th: int, sw: int, sh: int) -> dict:
    cx = result["left"] + tw / 2.0
    cy = result["top"] + th / 2.0
    score = max(0.0, min(1.0, result["score"]))
    return {
        "found": score >= float(threshold),
        "score": round(score, 4),
        "threshold": float(threshold),
        "x": cx / max(1, sw - 1),
        "y": cy / max(1, sh - 1),
        "px": int(round(cx)),
        "py": int(round(cy)),
        "left": int(result["left"]),
        "top": int(result["top"]),
        "right": int(result["right"]),
        "bottom": int(result["bottom"]),
        "search_mode": result.get("search_mode", "full"),
        "template": template_path,
        "template_width": tw,
        "template_height": th,
        "screen_width": sw,
        "screen_height": sh,
    }


def _predict_region(record_pos: Any, resolution: Any, sw: int, sh: int,
                    tw: int, th: int) -> tuple[int, int, int, int] | None:
    expected = _expected_top_left(record_pos, resolution, sw, sh, tw, th)
    if expected is None:
        return None
    left_at_center, top_at_center = expected
    cx = left_at_center + tw / 2
    cy = top_at_center + th / 2
    margin = max(64, int(max(tw, th) * 0.75), int(min(sw, sh) * 0.08))
    margin = min(margin, max(80, int(min(sw, sh) * 0.25)))
    left = int(cx - tw / 2 - margin)
    top = int(cy - th / 2 - margin)
    right = int(cx + tw / 2 + margin)
    bottom = int(cy + th / 2 + margin)
    return normalize_region((left, top, right, bottom), sw, sh)


def _expected_top_left(record_pos: Any, resolution: Any, sw: int, sh: int,
                       tw: int, th: int) -> tuple[int, int] | None:
    try:
        dx, dy = [float(v) for v in record_pos]
    except (TypeError, ValueError):
        return None
    if not (-2.0 <= dx <= 2.0 and -2.0 <= dy <= 2.0):
        return None
    try:
        rw, rh = [float(v) for v in resolution]
    except (TypeError, ValueError):
        rw, rh = sw, sh
    if rw <= 0 or rh <= 0:
        rw, rh = sw, sh
    rec_x = (0.5 + dx) * rw
    rec_y = (0.5 + dy) * rh
    cx = rec_x * sw / rw
    cy = rec_y * sh / rh
    left = int(round(cx - tw / 2))
    top = int(round(cy - th / 2))
    left = max(0, min(max(0, sw - tw), left))
    top = max(0, min(max(0, sh - th), top))
    return left, top


def _gray(pixel: tuple[int, int, int]) -> int:
    r, g, b = pixel
    return (r * 299 + g * 587 + b * 114) // 1000


def _score_at(screen, template, sx: int, sy: int,
              points: list[tuple[int, int]], rgb: bool = False) -> float:
    screen_vals = []
    template_vals = []
    for tx, ty in points:
        sr, sg, sb = screen[sy + ty][sx + tx]
        tr, tg, tb = template[ty][tx]
        if rgb:
            screen_vals.extend((sr, sg, sb))
            template_vals.extend((tr, tg, tb))
        else:
            screen_vals.append(_gray((sr, sg, sb)))
            template_vals.append(_gray((tr, tg, tb)))
    n = len(screen_vals)
    if n <= 1:
        return 0.0
    screen_mean = sum(screen_vals) / n
    template_mean = sum(template_vals) / n
    num = 0.0
    screen_den = 0.0
    template_den = 0.0
    for sv, tv in zip(screen_vals, template_vals):
        sd = sv - screen_mean
        td = tv - template_mean
        num += sd * td
        screen_den += sd * sd
        template_den += td * td
    if screen_den <= 1e-9 or template_den <= 1e-9:
        return 0.0
    return num / math.sqrt(screen_den * template_den)
