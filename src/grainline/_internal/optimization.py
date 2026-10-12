# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""
Extracted frontier orchestration and shared geometry/cleanup helpers.
All public search settings are supplied per call through SearchOptions.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import List, Dict, Any, Tuple, Optional

from grainline._internal.runtime import config
from grainline.options import SearchOptions
from grainline._internal.normalize import normalize_grain_direction
from grainline._internal.runtime import append_optimize_trace, get_request_id
from grainline._internal.banding import build_part_banding_payload, build_rotated_banding_payload, summarize_banding_from_board_results
from grainline._internal.grain_fit_rules import orientation_candidates


# Above this utilisation a lower sheet count is almost never packable, so the fixed-split fallback is skipped.
SHEET_PLAN_MAX_UTILISATION = 0.93


def _log_optimize_algo(event: str, **fields: Any) -> None:
    rid = get_request_id() or "-"
    extras = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    append_optimize_trace(
        "evt=optimize_algo rid=%s event=%s%s%s"
        % (rid, event, " " if extras else "", extras)
    )


def _blank_sheet_pair_can_fit(
    sheet_length: float,
    sheet_width: float,
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    w1 = float(first.get("width") or 0.0)
    h1 = float(first.get("height") or 0.0)
    w2 = float(second.get("width") or 0.0)
    h2 = float(second.get("height") or 0.0)
    if min(w1, h1, w2, h2) <= 0.0:
        return False
    if w1 + w2 <= float(sheet_length) and max(h1, h2) <= float(sheet_width):
        return True
    if h1 + h2 <= float(sheet_width) and max(w1, w2) <= float(sheet_length):
        return True
    return False


def _blank_sheet_singleton_lower_bound(
    parts: list[Any],
    *,
    sheet_length: float,
    sheet_width: float,
) -> tuple[int, list[dict[str, Any]]]:
    lower_bound = 1
    limiting_rows: list[dict[str, Any]] = []
    families: dict[tuple[tuple[float, float], ...], dict[str, Any]] = {}
    for part in parts or []:
        qty = int(getattr(part, "quantity", 1) or 1)
        raw_candidates = getattr(part, "candidates", None) or []
        candidates = sorted(
            [
                {
                    "width": float(cand.get("width") or 0.0),
                    "height": float(cand.get("height") or 0.0),
                    "rotated": bool(cand.get("rotated", False)),
                }
                for cand in raw_candidates
                if float(cand.get("width") or 0.0) > 0.0
                and float(cand.get("height") or 0.0) > 0.0
                and float(cand.get("width") or 0.0) <= float(sheet_length)
                and float(cand.get("height") or 0.0) <= float(sheet_width)
            ],
            key=lambda item: (float(item["width"]), float(item["height"])),
        )
        if not candidates:
            continue
        signature = tuple((float(cand["width"]), float(cand["height"])) for cand in candidates)
        family = families.setdefault(
            signature,
            {
                "quantity": 0,
                "candidates": candidates,
                "part_ids": [],
                "part_names": [],
            },
        )
        family["quantity"] += max(0, qty)
        if len(family["part_ids"]) < 6:
            family["part_ids"].append(str(getattr(part, "id", "") or ""))
        if len(family["part_names"]) < 3:
            family["part_names"].append(str(getattr(part, "name", "") or ""))
    for family in families.values():
        qty = int(family.get("quantity") or 0)
        if qty <= 1:
            continue
        candidates = list(family.get("candidates") or [])
        two_up_possible = any(
            _blank_sheet_pair_can_fit(sheet_length, sheet_width, first, second)
            for first in candidates
            for second in candidates
        )
        if two_up_possible:
            continue
        lower_bound = max(lower_bound, qty)
        limiting_rows.append(
            {
                "part_ids": list(family.get("part_ids") or []),
                "part_names": list(family.get("part_names") or []),
                "quantity": qty,
                "candidate_dims": [
                    {
                        "width": float(cand.get("width") or 0.0),
                        "height": float(cand.get("height") or 0.0),
                    }
                    for cand in candidates
                ],
            }
        )
    return lower_bound, limiting_rows


@dataclass
class Rect:
    """Rectangle representing a placed part."""
    x: float
    y: float
    width: float
    height: float
    part_id: str
    part_name: str
    rotated: bool = False


@dataclass
class Sheet:
    """Represents a sheet of material."""
    id: int
    placements: List[Rect]
    used_area: float
    total_area: float

    @property
    def utilization(self) -> float:
        return (self.used_area / self.total_area * 100) if self.total_area > 0 else 0


def _rect_value(rect: Any, key: str, default: float = 0.0) -> float:
    if isinstance(rect, dict):
        value = rect.get(key, default)
    else:
        value = getattr(rect, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _rect_x(rect: Any) -> float:
    return _rect_value(rect, "x")


def _rect_y(rect: Any) -> float:
    return _rect_value(rect, "y")


def _rect_width(rect: Any) -> float:
    return _rect_value(rect, "width")


def _rect_height(rect: Any) -> float:
    return _rect_value(rect, "height")


def _rect_area(rect: Any) -> float:
    area = _rect_value(rect, "area", float("nan"))
    if not math.isnan(area) and area > 0:
        return area
    return max(0.0, _rect_width(rect) * _rect_height(rect))


def _compute_offcut_metrics_from_rects(
    leftovers: list[dict[str, Any]] | list["FreeRect"],
    *,
    sheet_length: float | None = None,
    sheet_width: float | None = None,
) -> dict[str, float]:
    total_offcut_area = 0.0
    largest_offcut_area = 0.0
    reusable_offcut_area = 0.0
    reusable_offcut_count = 0.0
    skinny_scrap_area = 0.0
    skinny_scrap_count = 0.0
    offcut_fragment_count = 0.0
    offcut_squareness_score = 0.0
    edge_hugging_area = 0.0
    interior_void_area = 0.0
    downstream_offcut_area = 0.0
    bottom_right_offcut_area = 0.0

    for rect in leftovers or []:
        width = _rect_width(rect)
        height = _rect_height(rect)
        x = _rect_x(rect)
        y = _rect_y(rect)
        area = _rect_area(rect)
        if width <= 0 or height <= 0 or area <= 0:
            continue

        total_offcut_area += area
        offcut_fragment_count += 1.0
        largest_offcut_area = max(largest_offcut_area, area)
        min_side = min(width, height)
        max_side = max(width, height)
        aspect = max_side / max(min_side, 1e-9)
        offcut_squareness_score += area * min(1.0, min_side / max_side)

        if min_side >= 180.0 and aspect <= 4.5:
            reusable_offcut_area += area
            reusable_offcut_count += 1.0
        if min_side < 120.0 or aspect >= 8.0:
            skinny_scrap_area += area
            skinny_scrap_count += 1.0

        if sheet_length is not None and sheet_width is not None:
            touches_right = abs((x + width) - float(sheet_length)) <= 1e-6
            touches_bottom = abs((y + height) - float(sheet_width)) <= 1e-6
            touches_edge = (
                abs(x) <= 1e-6
                or abs(y) <= 1e-6
                or touches_right
                or touches_bottom
            )
            if touches_edge:
                edge_hugging_area += area
            else:
                interior_void_area += area
            if touches_right or touches_bottom:
                downstream_offcut_area = max(downstream_offcut_area, area)
            if touches_right and touches_bottom:
                bottom_right_offcut_area = max(bottom_right_offcut_area, area)

    return {
        "total_offcut_area": total_offcut_area,
        "largest_offcut_area": largest_offcut_area,
        "reusable_offcut_area": reusable_offcut_area,
        "reusable_offcut_count": reusable_offcut_count,
        "skinny_scrap_area": skinny_scrap_area,
        "skinny_scrap_count": skinny_scrap_count,
        "offcut_fragment_count": offcut_fragment_count,
        "offcut_squareness_score": offcut_squareness_score,
        "edge_hugging_area": edge_hugging_area,
        "interior_void_area": interior_void_area,
        "downstream_offcut_area": downstream_offcut_area,
        "bottom_right_offcut_area": bottom_right_offcut_area,
        "main_offcut_ratio": (largest_offcut_area / total_offcut_area) if total_offcut_area > 0 else 0.0,
    }


def _merge_offcut_metrics(metrics_list: list[dict[str, float]]) -> dict[str, float]:
    merged: dict[str, float] = {
        "total_offcut_area": 0.0,
        "largest_offcut_area": 0.0,
        "reusable_offcut_area": 0.0,
        "reusable_offcut_count": 0.0,
        "skinny_scrap_area": 0.0,
        "skinny_scrap_count": 0.0,
        "offcut_fragment_count": 0.0,
        "offcut_squareness_score": 0.0,
        "edge_hugging_area": 0.0,
        "interior_void_area": 0.0,
        "downstream_offcut_area": 0.0,
        "bottom_right_offcut_area": 0.0,
        "main_offcut_ratio": 0.0,
    }
    for metrics in metrics_list:
        for key in merged:
            if key == "largest_offcut_area":
                merged[key] = max(merged[key], float(metrics.get(key) or 0.0))
            elif key in ("downstream_offcut_area", "bottom_right_offcut_area"):
                merged[key] = max(merged[key], float(metrics.get(key) or 0.0))
            elif key == "main_offcut_ratio":
                continue
            else:
                merged[key] += float(metrics.get(key) or 0.0)
    total_offcut_area = float(merged.get("total_offcut_area") or 0.0)
    largest = float(merged.get("largest_offcut_area") or 0.0)
    merged["main_offcut_ratio"] = (largest / total_offcut_area) if total_offcut_area > 0 else 0.0
    return merged


def _offcut_quality_key(metrics: dict[str, float]) -> tuple[float, ...]:
    return (
        -float(metrics.get("bottom_right_offcut_area") or 0.0),
        -float(metrics.get("downstream_offcut_area") or 0.0),
        -float(metrics.get("main_offcut_ratio") or 0.0),
        float(metrics.get("offcut_fragment_count") or 0.0),
        float(metrics.get("skinny_scrap_area") or 0.0),
        float(metrics.get("interior_void_area") or 0.0),
        -float(metrics.get("largest_offcut_area") or 0.0),
        -float(metrics.get("offcut_squareness_score") or 0.0),
        -float(metrics.get("reusable_offcut_area") or 0.0),
        -float(metrics.get("edge_hugging_area") or 0.0),
    )


def _compute_leftovers_from_placements(
    placements: list["Rect"] | list[dict[str, Any]],
    *,
    sheet_length: float,
    sheet_width: float,
) -> list[dict[str, float]]:
    free_rects: list[dict[str, float]] = [
        {"x": 0.0, "y": 0.0, "width": float(sheet_length), "height": float(sheet_width)}
    ]

    def _intersects(free_rect: dict[str, float], rect: "Rect" | dict[str, Any]) -> bool:
        fr_x = float(free_rect["x"])
        fr_y = float(free_rect["y"])
        fr_w = float(free_rect["width"])
        fr_h = float(free_rect["height"])
        rect_x = _rect_x(rect)
        rect_y = _rect_y(rect)
        rect_w = _rect_width(rect)
        rect_h = _rect_height(rect)
        return not (
            (rect_x + rect_w) <= fr_x + 1e-9
            or rect_x >= (fr_x + fr_w) - 1e-9
            or (rect_y + rect_h) <= fr_y + 1e-9
            or rect_y >= (fr_y + fr_h) - 1e-9
        )

    def _split(free_rect: dict[str, float], rect: "Rect" | dict[str, Any]) -> list[dict[str, float]]:
        left = _rect_x(rect)
        top = _rect_y(rect)
        right = left + _rect_width(rect)
        bottom = top + _rect_height(rect)

        fr_x = float(free_rect["x"])
        fr_y = float(free_rect["y"])
        fr_w = float(free_rect["width"])
        fr_h = float(free_rect["height"])
        fr_right = fr_x + fr_w
        fr_bottom = fr_y + fr_h
        new_rects: list[dict[str, float]] = []

        if top > fr_y + 1e-9:
            new_rects.append({"x": fr_x, "y": fr_y, "width": fr_w, "height": top - fr_y})
        if bottom < fr_bottom - 1e-9:
            new_rects.append({"x": fr_x, "y": bottom, "width": fr_w, "height": fr_bottom - bottom})
        if left > fr_x + 1e-9:
            new_rects.append({"x": fr_x, "y": fr_y, "width": left - fr_x, "height": fr_h})
        if right < fr_right - 1e-9:
            new_rects.append({"x": right, "y": fr_y, "width": fr_right - right, "height": fr_h})

        return [r for r in new_rects if float(r["width"]) > 1e-9 and float(r["height"]) > 1e-9]

    def _prune(rects: list[dict[str, float]]) -> list[dict[str, float]]:
        kept: list[dict[str, float]] = []
        for i, rect in enumerate(rects):
            rect_x = float(rect["x"])
            rect_y = float(rect["y"])
            rect_w = float(rect["width"])
            rect_h = float(rect["height"])
            contained = False
            for j, other in enumerate(rects):
                if i == j:
                    continue
                other_x = float(other["x"])
                other_y = float(other["y"])
                other_w = float(other["width"])
                other_h = float(other["height"])
                if (
                    rect_x >= other_x - 1e-9
                    and rect_y >= other_y - 1e-9
                    and (rect_x + rect_w) <= (other_x + other_w) + 1e-9
                    and (rect_y + rect_h) <= (other_y + other_h) + 1e-9
                ):
                    contained = True
                    break
            if not contained:
                kept.append(rect)
        kept.sort(key=lambda item: (float(item["y"]), float(item["x"]), -(float(item["width"]) * float(item["height"]))))
        return kept

    for rect in sorted(
        placements,
        key=lambda item: (_rect_y(item), _rect_x(item), -(_rect_width(item) * _rect_height(item))),
    ):
        next_free: list[dict[str, float]] = []
        for free_rect in free_rects:
            if not _intersects(free_rect, rect):
                next_free.append(free_rect)
                continue
            next_free.extend(_split(free_rect, rect))
        free_rects = _prune(next_free)

    return [
        {
            "x": float(rect["x"]),
            "y": float(rect["y"]),
            "width": float(rect["width"]),
            "height": float(rect["height"]),
            "area": float(rect["width"]) * float(rect["height"]),
        }
        for rect in _prune(free_rects)
    ]


def _rects_overlap(a: "Rect", b: "Rect") -> bool:
    return not (
        a.x + a.width <= b.x + 1e-6
        or b.x + b.width <= a.x + 1e-6
        or a.y + a.height <= b.y + 1e-6
        or b.y + b.height <= a.y + 1e-6
    )


def _non_overlapping(rect: "Rect", others: list["Rect"]) -> bool:
    return all(not _rects_overlap(rect, other) for other in others)


def _compute_placement_compactness_metrics(placements: list["Rect"] | list[dict[str, Any]]) -> dict[str, float]:
    if not placements:
        return {
            "bbox_area": 0.0,
            "bbox_void_area": 0.0,
            "bbox_width": 0.0,
            "bbox_height": 0.0,
        }

    min_x = min(_rect_x(p) for p in placements)
    min_y = min(_rect_y(p) for p in placements)
    max_x = max(_rect_x(p) + _rect_width(p) for p in placements)
    max_y = max(_rect_y(p) + _rect_height(p) for p in placements)
    bbox_width = max(0.0, max_x - min_x)
    bbox_height = max(0.0, max_y - min_y)
    bbox_area = bbox_width * bbox_height
    used_area = sum(_rect_width(p) * _rect_height(p) for p in placements)
    bbox_void_area = max(0.0, bbox_area - used_area)
    return {
        "bbox_area": bbox_area,
        "bbox_void_area": bbox_void_area,
        "bbox_width": bbox_width,
        "bbox_height": bbox_height,
    }


def _compute_layout_cohesion_metrics(
    placements: list["Rect"] | list[dict[str, Any]],
    *,
    sheet_length: float,
    sheet_width: float,
) -> dict[str, float]:
    if not placements:
        return {
            "edge_contact": 0.0,
            "adjacency_contact": 0.0,
        }

    edge_contact = 0.0
    adjacency_contact = 0.0
    eps = 1e-6

    for i, rect in enumerate(placements):
        rect_x = _rect_x(rect)
        rect_y = _rect_y(rect)
        rect_w = _rect_width(rect)
        rect_h = _rect_height(rect)
        if abs(rect_x) <= eps:
            edge_contact += rect_h
        if abs(rect_y) <= eps:
            edge_contact += rect_w
        if abs((rect_x + rect_w) - float(sheet_length)) <= eps:
            edge_contact += rect_h
        if abs((rect_y + rect_h) - float(sheet_width)) <= eps:
            edge_contact += rect_w

        for other in placements[i + 1 :]:
            other_x = _rect_x(other)
            other_y = _rect_y(other)
            other_w = _rect_width(other)
            other_h = _rect_height(other)
            if abs((rect_x + rect_w) - other_x) <= eps or abs((other_x + other_w) - rect_x) <= eps:
                overlap = min(rect_y + rect_h, other_y + other_h) - max(rect_y, other_y)
                if overlap > eps:
                    adjacency_contact += overlap
            if abs((rect_y + rect_h) - other_y) <= eps or abs((other_y + other_h) - rect_y) <= eps:
                overlap = min(rect_x + rect_w, other_x + other_w) - max(rect_x, other_x)
                if overlap > eps:
                    adjacency_contact += overlap

    return {
        "edge_contact": edge_contact,
        "adjacency_contact": adjacency_contact,
    }


def _compute_support_gap_metrics(
    placements: list["Rect"] | list[dict[str, Any]],
    *,
    sheet_length: float,
    sheet_width: float,
) -> dict[str, float]:
    if not placements:
        return {
            "unsupported_below_area": 0.0,
            "unsupported_right_area": 0.0,
            "max_drop_gap": 0.0,
            "max_right_gap": 0.0,
        }

    unsupported_below_area = 0.0
    unsupported_right_area = 0.0
    max_drop_gap = 0.0
    max_right_gap = 0.0
    eps = 1e-6

    for rect in placements:
        x1 = _rect_x(rect)
        x2 = _rect_x(rect) + _rect_width(rect)
        y1 = _rect_y(rect)
        y2 = _rect_y(rect) + _rect_height(rect)

        support_below = float(sheet_width)
        for other in placements:
            if other is rect:
                continue
            ox1 = _rect_x(other)
            ox2 = _rect_x(other) + _rect_width(other)
            if min(x2, ox2) - max(x1, ox1) <= eps:
                continue
            other_y = _rect_y(other)
            if other_y >= y2 - eps:
                support_below = min(support_below, other_y)
        drop_gap = max(0.0, support_below - y2)
        if drop_gap > eps:
            unsupported_below_area += _rect_width(rect) * drop_gap
            max_drop_gap = max(max_drop_gap, drop_gap)

        support_right = float(sheet_length)
        for other in placements:
            if other is rect:
                continue
            oy1 = _rect_y(other)
            oy2 = _rect_y(other) + _rect_height(other)
            if min(y2, oy2) - max(y1, oy1) <= eps:
                continue
            other_x = _rect_x(other)
            if other_x >= x2 - eps:
                support_right = min(support_right, other_x)
        right_gap = max(0.0, support_right - x2)
        if right_gap > eps:
            unsupported_right_area += _rect_height(rect) * right_gap
            max_right_gap = max(max_right_gap, right_gap)

    return {
        "unsupported_below_area": unsupported_below_area,
        "unsupported_right_area": unsupported_right_area,
        "max_drop_gap": max_drop_gap,
        "max_right_gap": max_right_gap,
    }


def _compute_cut_workflow_metrics(placements: list["Rect"] | list[dict[str, Any]]) -> dict[str, float]:
    if not placements:
        return {
            "vertical_strip_area": 0.0,
            "horizontal_strip_area": 0.0,
            "dominant_strip_area": 0.0,
            "dominant_strip_ratio": 0.0,
            "strip_switch_penalty": 0.0,
            "singleton_strip_area": 0.0,
        }

    def _group_area(keys: dict[tuple[float, float], list["Rect"] | list[dict[str, Any]]]) -> tuple[float, float]:
        grouped_area = 0.0
        singleton_area = 0.0
        for rects in keys.values():
            area = sum(_rect_width(r) * _rect_height(r) for r in rects)
            if len(rects) > 1:
                grouped_area += area
            else:
                singleton_area += area
        return grouped_area, singleton_area

    vertical_groups: dict[tuple[float, float], list["Rect"] | list[dict[str, Any]]] = {}
    horizontal_groups: dict[tuple[float, float], list["Rect"] | list[dict[str, Any]]] = {}
    total_area = sum(_rect_width(r) * _rect_height(r) for r in placements)

    for rect in placements:
        vertical_groups.setdefault((round(_rect_x(rect), 4), round(_rect_width(rect), 4)), []).append(rect)
        horizontal_groups.setdefault((round(_rect_y(rect), 4), round(_rect_height(rect), 4)), []).append(rect)

    vertical_group_area, vertical_singleton_area = _group_area(vertical_groups)
    horizontal_group_area, horizontal_singleton_area = _group_area(horizontal_groups)
    dominant_area = max(vertical_group_area, horizontal_group_area)
    dominant_ratio = (dominant_area / total_area) if total_area > 0 else 0.0
    strip_switch_penalty = abs(vertical_group_area - horizontal_group_area)

    return {
        "vertical_strip_area": vertical_group_area,
        "horizontal_strip_area": horizontal_group_area,
        "dominant_strip_area": dominant_area,
        "dominant_strip_ratio": dominant_ratio,
        "strip_switch_penalty": strip_switch_penalty,
        "singleton_strip_area": min(vertical_singleton_area, horizontal_singleton_area),
    }


def _strict_guillotine_metrics(placements: list[dict[str, Any]], *, sheet_length: float, sheet_width: float) -> dict[str, int | bool]:
    rects = [
        {
            "x": float(item.get("x") or 0.0),
            "y": float(item.get("y") or 0.0),
            "width": float(item.get("width") or 0.0),
            "height": float(item.get("height") or 0.0),
        }
        for item in placements
    ]
    memo: dict[tuple, tuple[bool, int, int]] = {}

    def _serialize(items: list[dict], x0: float, y0: float, x1: float, y1: float) -> tuple:
        return (
            round(x0, 4),
            round(y0, 4),
            round(x1, 4),
            round(y1, 4),
            tuple(
                sorted(
                    (
                        round(float(item["x"]), 4),
                        round(float(item["y"]), 4),
                        round(float(item["width"]), 4),
                        round(float(item["height"]), 4),
                    )
                    for item in items
                )
            ),
        )

    def _solve(items: list[dict], x0: float, y0: float, x1: float, y1: float) -> tuple[bool, int, int]:
        key = _serialize(items, x0, y0, x1, y1)
        if key in memo:
            return memo[key]
        if len(items) <= 1:
            memo[key] = (True, len(items), 0)
            return memo[key]

        x_cuts = sorted(
            {
                round(float(item["x"]), 6)
                for item in items
                if x0 + 1e-6 < float(item["x"]) < x1 - 1e-6
            }
            | {
                round(float(item["x"]) + float(item["width"]), 6)
                for item in items
                if x0 + 1e-6 < float(item["x"]) + float(item["width"]) < x1 - 1e-6
            }
        )
        y_cuts = sorted(
            {
                round(float(item["y"]), 6)
                for item in items
                if y0 + 1e-6 < float(item["y"]) < y1 - 1e-6
            }
            | {
                round(float(item["y"]) + float(item["height"]), 6)
                for item in items
                if y0 + 1e-6 < float(item["y"]) + float(item["height"]) < y1 - 1e-6
            }
        )

        best_released = 0
        best_depth = 0

        for cut_x in x_cuts:
            crossing = [
                item
                for item in items
                if float(item["x"]) + 1e-6 < cut_x < float(item["x"]) + float(item["width"]) - 1e-6
            ]
            if crossing:
                continue
            left = [item for item in items if float(item["x"]) + float(item["width"]) <= cut_x + 1e-6]
            right = [item for item in items if float(item["x"]) >= cut_x - 1e-6]
            if not left or not right or len(left) + len(right) != len(items):
                continue
            left_ok, left_released, left_depth = _solve(left, x0, y0, cut_x, y1)
            right_ok, right_released, right_depth = _solve(right, cut_x, y0, x1, y1)
            best_released = max(best_released, left_released + right_released)
            best_depth = max(best_depth, 1 + max(left_depth, right_depth))
            if left_ok and right_ok:
                memo[key] = (True, len(items), 1 + max(left_depth, right_depth))
                return memo[key]

        for cut_y in y_cuts:
            crossing = [
                item
                for item in items
                if float(item["y"]) + 1e-6 < cut_y < float(item["y"]) + float(item["height"]) - 1e-6
            ]
            if crossing:
                continue
            bottom = [item for item in items if float(item["y"]) + float(item["height"]) <= cut_y + 1e-6]
            top = [item for item in items if float(item["y"]) >= cut_y - 1e-6]
            if not bottom or not top or len(bottom) + len(top) != len(items):
                continue
            bottom_ok, bottom_released, bottom_depth = _solve(bottom, x0, y0, x1, cut_y)
            top_ok, top_released, top_depth = _solve(top, x0, cut_y, x1, y1)
            best_released = max(best_released, bottom_released + top_released)
            best_depth = max(best_depth, 1 + max(bottom_depth, top_depth))
            if bottom_ok and top_ok:
                memo[key] = (True, len(items), 1 + max(bottom_depth, top_depth))
                return memo[key]

        memo[key] = (False, max(best_released, 1 if items else 0), best_depth)
        return memo[key]

    is_guillotine, released, depth = _solve(rects, 0.0, 0.0, sheet_length, sheet_width)
    return {
        "is_guillotine": bool(is_guillotine),
        "guillotine_release_count": int(released),
        "guillotine_unreleased_count": max(0, len(rects) - int(released)),
        "guillotine_depth": int(depth),
    }


def _strict_validate_result(res: dict[str, Any], *, sheet_length: float, sheet_width: float) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for sheet in list(res.get("sheets") or []):
        sheet_id = int(sheet.get("id") or 0)
        workflow = sheet.get("workflow_metrics") if isinstance(sheet.get("workflow_metrics"), dict) else None
        metrics = workflow or _strict_guillotine_metrics(
            list(sheet.get("placements") or []),
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        is_ok = bool(metrics.get("is_guillotine")) and int(metrics.get("guillotine_unreleased_count") or 0) == 0
        if not is_ok:
            failures.append(
                {
                    "sheet_id": sheet_id,
                    "is_guillotine": bool(metrics.get("is_guillotine")),
                    "guillotine_unreleased_count": int(metrics.get("guillotine_unreleased_count") or 0),
                    "guillotine_depth": int(metrics.get("guillotine_depth") or 0),
                }
            )
    return {
        "passed": len(failures) == 0,
        "failed_sheets": failures,
    }


def _cut_plan_rect_payload(rect: Any) -> dict[str, float]:
    return {
        "x": float(getattr(rect, "x", 0.0) if not isinstance(rect, dict) else rect.get("x", 0.0) or 0.0),
        "y": float(getattr(rect, "y", 0.0) if not isinstance(rect, dict) else rect.get("y", 0.0) or 0.0),
        "width": float(getattr(rect, "width", 0.0) if not isinstance(rect, dict) else rect.get("width", 0.0) or 0.0),
        "height": float(getattr(rect, "height", 0.0) if not isinstance(rect, dict) else rect.get("height", 0.0) or 0.0),
    }


class ShelfAlgorithm:
    """Shelf algorithm for 2D bin packing."""

    def __init__(self, kerf_mm: float = None, allow_rotation: bool = True):
        self.kerf = float(kerf_mm) if kerf_mm is not None else config.optimization.default_kerf_mm
        self.allow_rotation = allow_rotation
        self.max_sheets = config.optimization.max_sheets

    def _reserve_kerf(self, remaining: float) -> float:
        # Product-friendly kerf rule:
        # Only reserve a kerf gap when there is enough remaining space to actually represent it.
        # If the leftover is smaller than the kerf, treat it as a tiny offcut/edge slack instead of forcing a new sheet.
        eps = 1e-9
        return float(self.kerf) if remaining > float(self.kerf) + eps else 0.0

    def optimize_sheet(
        self,
        parts: List[Dict[str, Any]],
        sheet_length: float,
        sheet_width: float
    ) -> Tuple[List[Rect], List[Dict]]:
        """Optimize placement of parts on a single sheet."""
        def _sort_key(p: Dict[str, Any]) -> float:
            cands = p.get("candidates") or []
            if not cands:
                return 0
            biggest = max((max(c["width"], c["height"]) for c in cands), default=0)
            return biggest

        sorted_parts = sorted(parts, key=_sort_key, reverse=True)

        shelves: List[Dict] = []
        placements: List[Rect] = []
        unplaced: List[Dict] = []

        for part in sorted_parts:
            candidates = part.get("candidates") or [
                {"width": part.get("width", 0), "height": part.get("height", 0), "rotated": False}
            ]
            placed = False

            for shelf in shelves:
                for cand in candidates:
                    w = float(cand["width"])
                    h = float(cand["height"])
                    rem_w = float(shelf["remaining_width"]) - w
                    pw = w + self._reserve_kerf(rem_w)
                    ph = h
                    if shelf['remaining_width'] >= pw and ph <= shelf['height']:
                        placements.append(Rect(
                            x=shelf['current_x'],
                            y=shelf['y'],
                            width=w,
                            height=h,
                            part_id=part['id'],
                            part_name=part['name'],
                            rotated=bool(cand.get("rotated", False))
                        ))
                        shelf['current_x'] += pw
                        shelf['remaining_width'] -= pw
                        placed = True
                        break
                if placed:
                    break

            if not placed:
                new_y = shelves[-1]['y'] + shelves[-1]['height'] if shelves else 0
                for cand in candidates:
                    w = float(cand["width"])
                    h = float(cand["height"])
                    rem_w = float(sheet_length) - w
                    rem_h = float(sheet_width) - (new_y + h)
                    pw = w + self._reserve_kerf(rem_w)
                    ph = h + self._reserve_kerf(rem_h)
                    if pw > sheet_length or ph > sheet_width:
                        continue
                    if new_y + ph <= sheet_width:
                        shelves.append({
                            'y': new_y,
                            'height': ph,
                            'current_x': pw,
                            'remaining_width': sheet_length - pw
                        })
                        placements.append(Rect(
                            x=0,
                            y=new_y,
                            width=w,
                            height=h,
                            part_id=part['id'],
                            part_name=part['name'],
                            rotated=bool(cand.get("rotated", False))
                        ))
                        placed = True
                        break

                if not placed:
                    unplaced.append(part)

        return placements, unplaced

    def optimize(
        self,
        parts: List[Any],
        boards: List[Any],
        sheet_length: float,
        sheet_width: float,
        max_sheets: int = None
    ) -> Dict[str, Any]:
        """Full optimization across multiple sheets."""
        max_sheets = max_sheets or self.max_sheets

        # Expand parts by quantity
        expanded_parts = []
        for part in parts:
            qty = getattr(part, 'quantity', 1)
            for i in range(qty):
                expanded_parts.append({
                    'id': str(getattr(part, 'id', i)),
                    'name': getattr(part, 'name', f'Part'),
                    # legacy fields kept for fallback
                    'width': float(getattr(part, 'length_mm', 0)),
                    'height': float(getattr(part, 'width_mm', 0)),
                    # candidates may be injected upstream
                    'candidates': getattr(part, 'candidates', None),
                    'original': part
                })

        if len(expanded_parts) > config.optimization.max_parts:
            raise ValueError(
                f"Too many parts after quantity expansion: {len(expanded_parts)}. Maximum: {config.optimization.max_parts}"
            )

        total_parts_area = sum(p['width'] * p['height'] for p in expanded_parts)

        # Run multi-sheet optimization
        all_sheets: List[Sheet] = []
        remaining = expanded_parts
        sheet_id = 0
        # No inventory logic; quantity is not part of Board model.
        max_available = max_sheets

        while remaining and sheet_id < max_available:
            sheet_id += 1
            placements, unplaced = self.optimize_sheet(
                remaining, sheet_length, sheet_width
            )

            if placements:
                used_area = sum(p.width * p.height for p in placements)
                sheet = Sheet(
                    id=sheet_id,
                    placements=placements,
                    used_area=used_area,
                    total_area=sheet_length * sheet_width
                )
                all_sheets.append(sheet)

            remaining = unplaced
            if not remaining:
                break

        total_used = sum(s.used_area for s in all_sheets)
        total_available = sum(s.total_area for s in all_sheets)
        overall_efficiency = (total_used / total_available * 100) if total_available > 0 else 0

        sheets_payload = []
        for s in all_sheets:
            placements_payload = [
                {
                    'part_id': p.part_id,
                    'part_name': p.part_name,
                    'x': p.x,
                    'y': p.y,
                    'width': p.width,
                    'height': p.height,
                    'rotated': p.rotated
                }
                for p in s.placements
            ]
            leftovers = _compute_leftovers_from_placements(
                placements_payload,
                sheet_length=float(sheet_length),
                sheet_width=float(sheet_width),
            )
            sheets_payload.append(
                {
                    'id': s.id,
                    'placements': placements_payload,
                    'utilization': round(s.utilization, 2),
                    'used_area': s.used_area,
                    'total_area': s.total_area,
                    'leftovers': leftovers,
                    'offcut_metrics': _compute_offcut_metrics_from_rects(
                        leftovers,
                        sheet_length=float(sheet_length),
                        sheet_width=float(sheet_width),
                    ),
                }
            )

        return {
            'sheets': sheets_payload,
            'sheets_used': len(all_sheets),
            'sheets_available': max_available,
            'unplaced_count': len(remaining),
            'efficiency_percent': round(overall_efficiency, 2),
            'total_parts_area': total_parts_area,
            'total_used_area': total_used,
            'kerf_mm': self.kerf,
            'algorithm': 'shelf',
            'offcut_metrics': _merge_offcut_metrics(
                [
                    _compute_offcut_metrics_from_rects(
                        sheet.get('leftovers') or [],
                        sheet_length=float(sheet_length),
                        sheet_width=float(sheet_width),
                    )
                    for sheet in sheets_payload
                ]
            ),
        }






def run_optimization(
    parts: List[Any],
    boards: List[Any],
    kerf_mm: float = None,
    algorithm: str = None,
    options: SearchOptions | None = None,
) -> Dict[str, Any]:
    """Internal orchestration, isolated from application configuration and storage."""
    options = options or SearchOptions()
    timeout_seconds = options.time_limit
    deadline = None if timeout_seconds is None else time.perf_counter() + timeout_seconds
    if not parts:
        raise ValueError("No parts provided for optimization")

    if not boards:
        raise ValueError("No boards defined")

    def _usable_dims(board_obj) -> tuple[float, float]:
        length = float(getattr(board_obj, "length_mm"))
        width = float(getattr(board_obj, "width_mm"))
        trim_left = float(getattr(board_obj, "trim_left", 0) or 0)
        trim_right = float(getattr(board_obj, "trim_right", 0) or 0)
        trim_top = float(getattr(board_obj, "trim_top", 0) or 0)
        trim_bottom = float(getattr(board_obj, "trim_bottom", 0) or 0)
        usable_length = length - trim_left - trim_right
        usable_width = width - trim_top - trim_bottom
        if usable_length <= 0 or usable_width <= 0:
            raise ValueError("Board trim leaves no usable area")
        return usable_length, usable_width

    def _orientation_candidates(board_obj, part_obj) -> list[dict]:
        return orientation_candidates(board_obj, part_obj)

    def _cut_size_proxy(part_obj: Any) -> Any:
        banding_payload = getattr(part_obj, "banding_payload", None)
        if not isinstance(banding_payload, dict):
            banding_payload = build_part_banding_payload(part_obj, {})
        cut_length = float(banding_payload.get("cut_length_mm") or getattr(part_obj, "length_mm", 0) or 0)
        cut_width = float(banding_payload.get("cut_width_mm") or getattr(part_obj, "width_mm", 0) or 0)
        class PartProxy:
            pass
        proxy = PartProxy()
        proxy.id = getattr(part_obj, "id", None)
        proxy.project_id = getattr(part_obj, "project_id", None)
        proxy.board_id = getattr(part_obj, "board_id", None)
        proxy.name = getattr(part_obj, "name", "Part")
        proxy.length_mm = cut_length
        proxy.width_mm = cut_width
        proxy.finished_length_mm = float(getattr(part_obj, "length_mm", 0) or 0)
        proxy.finished_width_mm = float(getattr(part_obj, "width_mm", 0) or 0)
        proxy.thickness_mm = getattr(part_obj, "thickness_mm", None)
        proxy.quantity = int(getattr(part_obj, "quantity", 1) or 1)
        proxy.grain_direction = getattr(part_obj, "grain_direction", "any")
        proxy.banding_payload = banding_payload
        return proxy

    def _cut_part_area(part_obj: Any) -> float:
        banding_payload = getattr(part_obj, "banding_payload", None)
        if not isinstance(banding_payload, dict):
            banding_payload = build_part_banding_payload(part_obj, {})
        cut_length = float(banding_payload.get("cut_length_mm") or getattr(part_obj, "length_mm", 0) or 0)
        cut_width = float(banding_payload.get("cut_width_mm") or getattr(part_obj, "width_mm", 0) or 0)
        return cut_length * cut_width * float(getattr(part_obj, "quantity", 1) or 1)

    def _freeze_part_snapshot_map(part_rows: list[Any]) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for part in part_rows or []:
            part_id = str(getattr(part, "id", "") or "")
            if not part_id:
                continue
            banding_payload = build_part_banding_payload(part, {})
            if isinstance(getattr(part, "banding_payload", None), dict):
                banding_payload = dict(getattr(part, "banding_payload"))
            snapshot[part_id] = {
                "part_name": str(getattr(part, "name", "") or "Part"),
                "length_mm": float(getattr(part, "length_mm", 0) or 0),
                "width_mm": float(getattr(part, "width_mm", 0) or 0),
                "cut_length_mm": float(banding_payload.get("cut_length_mm") or getattr(part, "length_mm", 0) or 0),
                "cut_width_mm": float(banding_payload.get("cut_width_mm") or getattr(part, "width_mm", 0) or 0),
                "grain_direction": normalize_grain_direction(getattr(part, "grain_direction", None), strict=False),
                "banding": banding_payload,
            }
        return snapshot

    def _attach_part_snapshot_to_layouts(layouts: list[dict[str, Any]], snapshot: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        next_layouts: list[dict[str, Any]] = []
        for sheet in layouts or []:
            if not isinstance(sheet, dict):
                continue
            next_sheet = dict(sheet)
            next_placements: list[dict[str, Any]] = []
            for placement in sheet.get("placements") or []:
                if not isinstance(placement, dict):
                    continue
                next_placement = dict(placement)
                part_id = str(next_placement.get("part_id") or "")
                metadata = snapshot.get(part_id)
                if metadata is None and ":" in part_id:
                    base_part_id, suffix = part_id.rsplit(":", 1)
                    if suffix.isdigit():
                        metadata = snapshot.get(base_part_id)
                if metadata:
                    next_placement["part_name"] = str(metadata.get("part_name") or next_placement.get("part_name") or "Part")
                    next_placement["length_mm"] = float(metadata.get("length_mm") or 0)
                    next_placement["width_mm"] = float(metadata.get("width_mm") or 0)
                    next_placement["cut_length_mm"] = float(metadata.get("cut_length_mm") or 0)
                    next_placement["cut_width_mm"] = float(metadata.get("cut_width_mm") or 0)
                    next_placement["grain_direction"] = str(metadata.get("grain_direction") or "any")
                    next_placement["banding"] = build_rotated_banding_payload(
                        metadata.get("banding"),
                        rotated=bool(next_placement.get("rotated")),
                    )
                next_placements.append(next_placement)
            next_sheet["placements"] = next_placements
            next_layouts.append(next_sheet)
        return next_layouts

    # Group by board_id
    boards_by_id = {str(getattr(b, "id")): b for b in boards}
    parts_by_board: dict[str, list[Any]] = {}
    for p in parts:
        bid = getattr(p, "board_id", None)
        if not bid:
            raise ValueError("All parts must be linked to a board")
        bid_str = str(bid)
        if bid_str not in boards_by_id:
            raise ValueError("Part references a board that is not in this project")
        parts_by_board.setdefault(bid_str, []).append(p)

    algorithm = algorithm or config.optimization.default_algorithm
    part_snapshot_by_id = _freeze_part_snapshot_map(list(parts or []))
    started_at = time.monotonic()
    timed_out = False
    _log_optimize_algo(
        "start",
        algorithm=algorithm,
        parts=len(list(parts or [])),
        boards=len(list(boards or [])),
        timeout_s=float(timeout_seconds) if timeout_seconds is not None else None,
    )

    def _check_timeout() -> bool:
        nonlocal timed_out
        if timeout_seconds is None:
            return False
        if (time.monotonic() - started_at) > float(timeout_seconds):
            timed_out = True
            return True
        return False

    def _remaining_budget_ms() -> float | None:
        if timeout_seconds is None:
            return None
        return max(0.0, (float(timeout_seconds) - (time.monotonic() - started_at)) * 1000.0)

    board_results = []
    board_telemetry: list[dict[str, Any]] = []
    chosen_algorithms: list[str] = []
    total_used_area = 0.0
    total_area = 0.0
    total_sheets_used = 0
    total_unplaced = 0

    for board_id, group_parts in parts_by_board.items():
        _check_timeout()
        board_start = time.monotonic()
        board = boards_by_id[board_id]
        usable_length, usable_width = _usable_dims(board)
        _log_optimize_algo(
            "board_start",
            board_id=board_id,
            parts=len(list(group_parts or [])),
            usable_length=int(usable_length),
            usable_width=int(usable_width),
        )

        # Inject orientation candidates without modifying ORM objects
        enriched_parts: list[Any] = []
        for p in group_parts:
            proxy = _cut_size_proxy(p)
            setattr(proxy, "candidates", _orientation_candidates(board, proxy))
            enriched_parts.append(proxy)

        def _offcut_metrics(sheets: list[dict[str, Any]]) -> dict[str, float]:
            per_sheet = [
                _compute_offcut_metrics_from_rects(
                    sheet.get("leftovers") or [],
                    sheet_length=float(usable_length),
                    sheet_width=float(usable_width),
                )
                for sheet in sheets
            ]
            merged = _merge_offcut_metrics(per_sheet)
            merged["largest_rect_sum"] = sum(float(m.get("largest_offcut_area") or 0.0) for m in per_sheet)
            merged["reusable_area_sum"] = float(merged["reusable_offcut_area"])
            merged["reusable_count"] = float(merged["reusable_offcut_count"])
            merged["skinny_area_sum"] = float(merged["skinny_scrap_area"])
            merged["skinny_count"] = float(merged["skinny_scrap_count"])
            merged["fragmentation_count"] = float(merged["offcut_fragment_count"])
            merged["squareness_bonus"] = float(merged["offcut_squareness_score"])
            return merged

        def _default_score(res: dict) -> tuple[int, int, float, int]:
            # Always prefer solutions that place all parts.
            # sheet count is only meaningful when unplaced_count == 0.
            unplaced = int(res.get("unplaced_count") or 0)
            sheets_used = int(res.get("sheets_used") or 0)
            sheets = res.get("sheets") or []
            sum_max_leftover = 0.0
            rect_count = 0
            for s in sheets:
                leftovers = s.get("leftovers") or []
                rect_count += len(leftovers)
                if leftovers:
                    sum_max_leftover += max((float(r.get("area") or 0.0) for r in leftovers), default=0.0)
            return (unplaced, sheets_used, -sum_max_leftover, rect_count)

        def _saw_friendly_score(res: dict) -> tuple[float, ...]:
            unplaced = int(res.get("unplaced_count") or 0)
            sheets_used = int(res.get("sheets_used") or 0)
            sheets = res.get("sheets") or []
            metrics = _offcut_metrics(sheets)
            total_waste = float(res.get("total_area") or 0.0) - float(res.get("total_used_area") or 0.0)
            total_waste = max(0.0, total_waste)
            early_sheet_void = 0.0
            early_sheet_total_offcut = 0.0
            early_reusable_offcut = 0.0
            early_edge_hugging_offcut = 0.0
            early_bbox_void = 0.0
            early_support_gaps = 0.0
            early_max_drop = 0.0
            early_right_gap = 0.0
            workflow_switch_penalty = 0.0
            workflow_singleton_area = 0.0
            dominant_strip_area = 0.0
            front_loaded_main_offcut = 0.0
            tail_part_count = float(len((sheets[-1].get("placements") or []))) if sheets else 0.0
            tail_metrics = _compute_offcut_metrics_from_rects(
                (sheets[-1].get("leftovers") or []) if sheets else [],
                sheet_length=float(usable_length),
                sheet_width=float(usable_width),
            )
            for idx, sheet in enumerate(sheets[:-1]):
                placements = sheet.get("placements") or []
                sheet_metrics = _compute_offcut_metrics_from_rects(
                    sheet.get("leftovers") or [],
                    sheet_length=float(usable_length),
                    sheet_width=float(usable_width),
                )
                compactness = _compute_placement_compactness_metrics(placements)
                support = _compute_support_gap_metrics(
                    placements,
                    sheet_length=float(usable_length),
                    sheet_width=float(usable_width),
                )
                workflow = _compute_cut_workflow_metrics(placements)
                early_sheet_total_offcut += float(sheet_metrics.get("total_offcut_area") or 0.0)
                early_reusable_offcut += float(sheet_metrics.get("reusable_offcut_area") or 0.0)
                early_edge_hugging_offcut += float(sheet_metrics.get("edge_hugging_area") or 0.0)
                if idx < max(0, len(sheets) - 2):
                    early_sheet_void += float(sheet_metrics.get("largest_offcut_area") or 0.0)
                    front_loaded_main_offcut += float(sheet_metrics.get("main_offcut_ratio") or 0.0)
                early_bbox_void += float(compactness.get("bbox_void_area") or 0.0)
                early_support_gaps += float(support.get("unsupported_below_area") or 0.0)
                early_max_drop = max(early_max_drop, float(support.get("max_drop_gap") or 0.0))
                early_right_gap += float(support.get("unsupported_right_area") or 0.0)
                workflow_switch_penalty += float(workflow.get("strip_switch_penalty") or 0.0)
                workflow_singleton_area += float(workflow.get("singleton_strip_area") or 0.0)
                dominant_strip_area += float(workflow.get("dominant_strip_area") or 0.0)
            return (
                float(unplaced),
                float(sheets_used),
                tail_part_count,
                *_offcut_quality_key(tail_metrics),
                early_sheet_total_offcut,
                early_reusable_offcut,
                early_edge_hugging_offcut,
                early_bbox_void,
                early_support_gaps,
                early_right_gap,
                early_max_drop,
                workflow_singleton_area,
                workflow_switch_penalty,
                early_sheet_void,
                front_loaded_main_offcut,
                -dominant_strip_area,
                metrics["interior_void_area"],
                metrics["skinny_count"],
                metrics["skinny_area_sum"],
                metrics["fragmentation_count"],
                -metrics["reusable_area_sum"],
                -metrics["squareness_bonus"],
                metrics["largest_rect_sum"],
                metrics["main_offcut_ratio"],
                total_waste,
            )

        score_fn = _saw_friendly_score if algorithm in ("saw_friendly", "adaptive_frontier_build_v1", "adaptive_frontier_release_v1") else _default_score

        sheet_area = float(usable_length) * float(usable_width) if usable_length and usable_width else 0.0
        total_area_parts = float(
            sum(
                _cut_part_area(p)
                for p in group_parts
            )
        )
        area_lb = int(math.ceil(total_area_parts / sheet_area)) if sheet_area > 0 else 1
        area_lb = max(1, area_lb)
        singleton_lb, singleton_limiting_rows = _blank_sheet_singleton_lower_bound(
            enriched_parts,
            sheet_length=float(usable_length),
            sheet_width=float(usable_width),
        )
        lb = max(1, area_lb, singleton_lb)

        # Cap the search window to keep runtime predictable.
        max_try = options.max_sheets

        from grainline._internal.adaptive_frontier_release_v1 import AdaptiveFrontierBuildV1Algorithm
        attempts = [("adaptive-frontier-release-v1", AdaptiveFrontierBuildV1Algorithm(
            kerf_mm=kerf_mm,
            seed_limit=options.seed_limit,
            candidate_pool_limit=options.candidate_pool_limit,
            random_seed=options.random_seed,
            partition_max_tries=options.partition_tries,
            seed_early_stop=options.seed_early_stop,
            deadline=deadline,
        ))]

        best_result = None
        best_score = None

        # Respect the user cap even when it is below the area lower bound.
        targets = range(min(lb, max_try), max_try + 1)
        target_values = list(targets)
        _log_optimize_algo(
            "search_window",
            board_id=board_id,
            lower_bound=lb,
            lower_bound_area=area_lb,
            lower_bound_singleton=singleton_lb,
            lower_bound_singleton_rows=len(singleton_limiting_rows),
            max_try=max_try,
            target_min=target_values[0] if target_values else None,
            target_max=target_values[-1] if target_values else None,
            target_count=len(target_values),
            attempts=len(attempts),
            remaining_ms=round(_remaining_budget_ms() or 0.0, 1) if timeout_seconds is not None else None,
        )

        found_perfect = False
        attempts_total = 0
        attempt_summaries: list[dict[str, Any]] = []
        for target in target_values:
            if _check_timeout() and best_result is not None:
                break
            target_start = time.monotonic()
            target_best_algo = None
            target_best_solved_result: dict[str, Any] | None = None
            target_best_solved_score: tuple[float, ...] | tuple[int, int, float, int] | None = None
            target_best_attempt_index: int | None = None
            for name, opt in attempts:
                _check_timeout()
                attempt_start = time.monotonic()
                remaining_ms = _remaining_budget_ms()
                res = opt.optimize(
                    parts=enriched_parts,
                    boards=[board],
                    sheet_length=usable_length,
                    sheet_width=usable_width,
                    max_sheets=target,
                )
                attempts_total += 1
                sc = score_fn(res)
                solved = int(res.get("unplaced_count") or 0) == 0
                is_new_best = best_score is None or sc < best_score
                if (not solved) and is_new_best:
                    best_score = sc
                    best_result = res
                    target_best_algo = str(name)
                    if isinstance(best_result, dict):
                        best_result["algorithm"] = str(name)
                if solved and (target_best_solved_score is None or sc < target_best_solved_score):
                    target_best_solved_result = res
                    target_best_solved_score = sc
                    target_best_attempt_index = len(attempt_summaries)
                    target_best_algo = str(name)
                _log_optimize_algo(
                    "attempt",
                    board_id=board_id,
                    target=target,
                    algo=name,
                    dt_ms=round((time.monotonic() - attempt_start) * 1000.0, 1),
                    sheets=int(res.get("sheets_used") or 0),
                    unplaced=int(res.get("unplaced_count") or 0),
                    won=is_new_best,
                    solved=solved,
                    remaining_ms=round(remaining_ms, 1) if remaining_ms is not None else None,
                )
                diagnostics = res.get("diagnostics") or {}
                target_dead_end = diagnostics.get("target_dead_end") if isinstance(diagnostics, dict) else None
                profile_summary = diagnostics.get("profile_summary") if isinstance(diagnostics, dict) else None
                seed_summaries = diagnostics.get("seed_summaries") if isinstance(diagnostics, dict) else None
                cleanup_decisions = diagnostics.get("cleanup_decisions") if isinstance(diagnostics, dict) else None
                board_structure_profile = diagnostics.get("board_structure_profile") if isinstance(diagnostics, dict) else None
                attempt_summaries.append(
                    {
                        "target": int(target),
                        "algorithm": str(res.get("algorithm") or name),
                        "dt_ms": round((time.monotonic() - attempt_start) * 1000.0, 1),
                        "sheets_used": int(res.get("sheets_used") or 0),
                        "unplaced_count": int(res.get("unplaced_count") or 0),
                        "solved": bool(solved),
                        "remaining_ms": round(remaining_ms, 1) if remaining_ms is not None else None,
                        "target_dead_end": target_dead_end if isinstance(target_dead_end, dict) else None,
                        "profile_summary": profile_summary if isinstance(profile_summary, dict) else None,
                        "seed_summaries": seed_summaries if isinstance(seed_summaries, list) else None,
                        "cleanup_decisions": cleanup_decisions if isinstance(cleanup_decisions, list) else None,
                        "board_structure_profile": board_structure_profile if isinstance(board_structure_profile, dict) else None,
                    }
                )
                if isinstance(target_dead_end, dict):
                    _log_optimize_algo(
                        "target_dead_end",
                        board_id=board_id,
                        target=target_dead_end.get("target") or target,
                        sheet_id=target_dead_end.get("sheet_id"),
                        reason=target_dead_end.get("reason"),
                        remaining_parts=target_dead_end.get("remaining_parts"),
                        remaining_area=target_dead_end.get("remaining_area"),
                        current_capacity=target_dead_end.get("current_capacity"),
                        future_capacity=target_dead_end.get("future_capacity"),
                        future_sheet_slots=target_dead_end.get("future_sheet_slots"),
                        fresh_only_parts=target_dead_end.get("fresh_only_parts"),
                        fresh_only_area=target_dead_end.get("fresh_only_area"),
                        oversized_fresh_only=target_dead_end.get("oversized_fresh_only"),
                        strip_parts=target_dead_end.get("strip_parts"),
                        strip_extent_required=target_dead_end.get("strip_extent_required"),
                        strip_extent_capacity=target_dead_end.get("strip_extent_capacity"),
                    )
            if target_best_solved_result is not None:
                strict_validation = _strict_validate_result(
                    target_best_solved_result,
                    sheet_length=float(usable_length),
                    sheet_width=float(usable_width),
                )
                if target_best_attempt_index is not None:
                    attempt_summaries[target_best_attempt_index]["strict_validation"] = strict_validation
                _log_optimize_algo(
                    "strict_validation",
                    board_id=board_id,
                    target=target,
                    passed=strict_validation.get("passed"),
                    failed_sheets=len(list(strict_validation.get("failed_sheets") or [])),
                )
                if bool(strict_validation.get("passed")):
                    best_score = target_best_solved_score
                    best_result = target_best_solved_result
                    if isinstance(best_result, dict):
                        best_result["algorithm"] = str(target_best_solved_result.get("algorithm") or target_best_algo or algorithm)
                    found_perfect = True
            _log_optimize_algo(
                "target_done",
                board_id=board_id,
                target=target,
                dt_ms=round((time.monotonic() - target_start) * 1000.0, 1),
                perfect=found_perfect,
                best_algo=target_best_algo,
            )
            if found_perfect:
                break

        result = best_result or attempts[0][1].optimize(
            parts=enriched_parts,
            boards=[board],
            sheet_length=usable_length,
            sheet_width=usable_width,
            max_sheets=max_try,
        )

        # The release algorithm commits one sheet at a time, so it can miss a lower sheet count that
        # needs hard parts and fillers shared out across sheets. When it lands above the lower bound,
        # look for such a split cheaply and let the same engine lay out each sheet. A result is only
        # taken when everything is placed and it passes the same strict validation as any winner.
        sheet_plan_summary: dict[str, Any] | None = None
        if (
            algorithm == "adaptive_frontier_release_v1"
            and options.partition_tries > 0
            and not _check_timeout()
            and attempts
            and int(result.get("unplaced_count") or 0) == 0
            and int(result.get("sheets_used") or 0) > lb
        ):
            current_sheets = int(result.get("sheets_used") or 0)
            plan_started = time.monotonic()
            sheet_plan_summary = {"from_sheets": current_sheets, "tried_targets": [], "accepted_target": None, "plans_laid_out": 0, "target_stats": []}
            # Trial counts are deterministic by default; optional wall-clock caps are explicit.
            fallback_budget_s = options.partition_time_limit
            for plan_target in range(lb, current_sheets):
                utilisation_needed = total_area_parts / (plan_target * sheet_area) if sheet_area > 0 else 1.0
                if utilisation_needed > SHEET_PLAN_MAX_UTILISATION:
                    continue
                budget_left_s = None if fallback_budget_s is None else fallback_budget_s - (time.monotonic() - plan_started)
                remaining_ms = _remaining_budget_ms()
                if remaining_ms is not None:
                    remaining_s = remaining_ms / 1000.0
                    budget_left_s = remaining_s if budget_left_s is None else min(budget_left_s, remaining_s)
                if budget_left_s is not None and budget_left_s <= 0:
                    break
                sheet_plan_summary["tried_targets"].append(int(plan_target))
                planned_result = attempts[0][1].optimize_with_sheet_plan(
                    parts=enriched_parts,
                    boards=[board],
                    sheet_length=usable_length,
                    sheet_width=usable_width,
                    sheet_count=plan_target,
                    layout_budget_s=budget_left_s,
                    time_budget_s=budget_left_s,
                    max_plans=options.partition_plans,
                )
                sheet_plan_summary["plans_laid_out"] += int(
                    (getattr(attempts[0][1], "last_sheet_plan_stats", None) or {}).get("plans_laid_out") or 0
                )
                sheet_plan_summary["target_stats"].append({
                    "target": int(plan_target),
                    **dict(getattr(attempts[0][1], "last_sheet_plan_stats", None) or {}),
                })
                if planned_result is None:
                    continue
                planned_validation = _strict_validate_result(
                    planned_result,
                    sheet_length=float(usable_length),
                    sheet_width=float(usable_width),
                )
                if not bool(planned_validation.get("passed")):
                    continue
                planned_result["algorithm"] = str(result.get("algorithm") or algorithm)
                result = planned_result
                sheet_plan_summary["accepted_target"] = int(plan_target)
                break
            sheet_plan_summary["dt_ms"] = round((time.monotonic() - plan_started) * 1000.0, 1)
            _log_optimize_algo(
                "sheet_plan_fallback",
                board_id=board_id,
                from_sheets=current_sheets,
                accepted_target=sheet_plan_summary["accepted_target"],
                tried_targets=len(sheet_plan_summary["tried_targets"]),
                dt_ms=sheet_plan_summary["dt_ms"],
            )

        _log_optimize_algo(
            "board_done",
            board_id=board_id,
            dt_ms=round((time.monotonic() - board_start) * 1000.0, 1),
            attempts=attempts_total,
            chosen=str(result.get("algorithm") or algorithm),
            sheets=int(result.get("sheets_used") or 0),
            unplaced=int(result.get("unplaced_count") or 0),
        )

        diagnostics = result.get("diagnostics") or {}
        profile_summary = diagnostics.get("profile_summary") if isinstance(diagnostics, dict) else None
        board_target_dead_end = diagnostics.get("target_dead_end") if isinstance(diagnostics, dict) else None
        seed_summaries = diagnostics.get("seed_summaries") if isinstance(diagnostics, dict) else None
        cleanup_decisions = diagnostics.get("cleanup_decisions") if isinstance(diagnostics, dict) else None
        board_structure_profile = diagnostics.get("board_structure_profile") if isinstance(diagnostics, dict) else None

        total_used_area += float(result.get("total_used_area") or 0)
        total_area += float(sum((s.get("total_area", 0) for s in result.get("sheets", []))))
        total_sheets_used += int(result.get("sheets_used") or 0)
        total_unplaced += int(result.get("unplaced_count") or 0)

        board_results.append({
            "board_id": board_id,
            "algorithm": str(result.get("algorithm") or algorithm),
            "required_sheets": int(result.get("sheets_used") or 0),
            "layouts": _attach_part_snapshot_to_layouts(result.get("sheets", []), part_snapshot_by_id),
            "unplaced_count": int(result.get("unplaced_count") or 0),
            "usable_length_mm": usable_length,
            "usable_width_mm": usable_width,
        })
        board_telemetry.append(
            {
                "board_id": board_id,
                "algorithm": str(result.get("algorithm") or algorithm),
                "lower_bound": int(lb),
                "lower_bound_area": int(area_lb),
                "lower_bound_singleton": int(singleton_lb),
                "lower_bound_singleton_rows": singleton_limiting_rows,
                "target_min": int(target_values[0]) if target_values else None,
                "target_max": int(target_values[-1]) if target_values else None,
                "attempts_total": int(attempts_total),
                "sheet_plan_fallback": sheet_plan_summary,
                "board_ms": round((time.monotonic() - board_start) * 1000.0, 1),
                "required_sheets": int(result.get("sheets_used") or 0),
                "unplaced_count": int(result.get("unplaced_count") or 0),
                "profile_summary": profile_summary if isinstance(profile_summary, dict) else None,
                "target_dead_end": board_target_dead_end if isinstance(board_target_dead_end, dict) else None,
                "seed_summaries": seed_summaries if isinstance(seed_summaries, list) else None,
                "cleanup_decisions": cleanup_decisions if isinstance(cleanup_decisions, list) else None,
                "board_structure_profile": board_structure_profile if isinstance(board_structure_profile, dict) else None,
                "sheet_equivalence_features": [
                    {
                        "sheet_id": sheet.get("id"),
                        "equivalence_features": sheet.get("equivalence_features"),
                    }
                    for sheet in (result.get("sheets") or [])
                    if isinstance(sheet, dict)
                ],
                "attempt_summaries": attempt_summaries,
            }
        )
        chosen_algorithms.append(str(result.get("algorithm") or algorithm))

    efficiency = (total_used_area / total_area * 100) if total_area > 0 else 0

    unique_algorithms = sorted({name for name in chosen_algorithms if name})
    result = {
        "boards": board_results,
        "sheets_used": total_sheets_used,
        "unplaced_count": total_unplaced,
        "efficiency_percent": round(efficiency, 2),
        "total_parts_area": float(sum((_cut_part_area(p) for p in parts))) if parts else 0.0,
        "total_used_area": float(total_used_area),
        "total_area": float(total_area),
        "total_waste_area": float(max(0.0, total_area - total_used_area)),
        "kerf_mm": float(kerf_mm) if kerf_mm is not None else float(config.optimization.default_kerf_mm),
        "algorithm": unique_algorithms[0] if len(unique_algorithms) == 1 else str(algorithm),
        "algorithm_impls": unique_algorithms,
        "banding_summary": summarize_banding_from_board_results(board_results),
        "telemetry": {
            "algorithm_ms": int(round((time.monotonic() - started_at) * 1000.0)),
            "board_summaries": board_telemetry,
            "board_groups": len(board_telemetry),
        },
    }
    _check_timeout()
    if timed_out:
        result["timed_out"] = True
    _log_optimize_algo(
        "done",
        dt_ms=round((time.monotonic() - started_at) * 1000.0, 1),
        sheets=int(result.get("sheets_used") or 0),
        unplaced=int(result.get("unplaced_count") or 0),
        algo=str(result.get("algorithm") or algorithm),
    )
    return result




@dataclass
class FreeRect:
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)




class GuillotineFFDAlgorithm:
    """
    Simple guillotine (binary-split) packing with First Fit Decreasing ordering.

    This is intentionally conservative and deterministic (good for hobbyist workflows).
    """

    def __init__(self, kerf_mm: float = None, allow_rotation: bool = True, sort_mode: str = "area"):
        self.kerf = float(kerf_mm) if kerf_mm is not None else float(config.optimization.default_kerf_mm)
        self.allow_rotation = bool(allow_rotation)
        self.max_sheets = int(config.optimization.max_sheets)
        self.sort_mode = str(sort_mode or "area").lower().strip()

    def _sort_key(self, p: Dict[str, Any]) -> tuple[float, float]:
        cands = p.get("candidates") or []
        if not cands:
            return (0.0, 0.0)

        widths = [float(c["width"]) for c in cands]
        heights = [float(c["height"]) for c in cands]
        areas = [w * h for w, h in zip(widths, heights)]
        biggest_area = max(areas) if areas else 0.0
        biggest_side = max((max(w, h) for w, h in zip(widths, heights)), default=0.0)
        smallest_side = max((min(w, h) for w, h in zip(widths, heights)), default=0.0)
        perimeter = max(((w + h) for w, h in zip(widths, heights)), default=0.0)

        mode = self.sort_mode
        if mode in ("max_side", "long_side"):
            return (biggest_side, biggest_area)
        if mode in ("short_side", "min_side"):
            return (smallest_side, biggest_area)
        if mode in ("perimeter",):
            return (perimeter, biggest_area)
        # default: area
        return (biggest_area, biggest_side)

    def _prune_free_rects(self, rects: list[FreeRect]) -> list[FreeRect]:
        rects = [r for r in rects if r.width > 1e-9 and r.height > 1e-9]
        pruned: list[FreeRect] = []
        for i, r in enumerate(rects):
            contained = False
            for j, o in enumerate(rects):
                if i == j:
                    continue
                if (
                    r.x >= o.x - 1e-9
                    and r.y >= o.y - 1e-9
                    and (r.x + r.width) <= (o.x + o.width) + 1e-9
                    and (r.y + r.height) <= (o.y + o.height) + 1e-9
                ):
                    contained = True
                    break
            if not contained:
                pruned.append(r)
        pruned.sort(key=lambda rr: (rr.y, rr.x, -rr.area))
        return pruned

    def _place_in_sheet(
        self,
        free_rects: list[FreeRect],
        part: Dict[str, Any],
        sheet_length: float,
        sheet_width: float,
        *,
        existing_placements: list[Rect] | None = None,
    ) -> tuple[Optional[Rect], list[FreeRect]]:
        """
        Returns (placement, new_free_rects). If placement is None, cannot place.
        """
        best = None
        best_placement: Optional[Rect] = None
        best_new_free = None
        best_meta: dict[str, Any] | None = None
        existing_placements = list(existing_placements or [])

        candidates = part.get("candidates") or [{"width": part.get("width", 0), "height": part.get("height", 0), "rotated": False}]
        if not self.allow_rotation:
            candidates = [c for c in candidates if not c.get("rotated")]

        def _leftover_score(leftovers: list[FreeRect]) -> tuple[float, ...]:
            metrics = _compute_offcut_metrics_from_rects(
                leftovers,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            waste = sum(r.area for r in leftovers)
            return (
                -float(metrics["reusable_offcut_area"]),
                float(metrics["interior_void_area"]),
                float(metrics["skinny_scrap_area"]),
                float(metrics["offcut_fragment_count"]),
                -float(metrics["offcut_squareness_score"]),
                float(metrics["largest_offcut_area"]),
                float(metrics["main_offcut_ratio"]),
                waste,
            )

        for fr_idx, fr in enumerate(free_rects):
            for cand in candidates:
                w = float(cand["width"])
                h = float(cand["height"])
                # Kerf behaves like a spacing on cut lines. If a part exactly touches a sheet edge,
                # do not reserve kerf on that side.
                rem_w = fr.width - w
                rem_h = fr.height - h
                # If the leftover is smaller than the kerf, treat it as a tiny offcut instead of forcing a new sheet.
                kx = self.kerf if rem_w > self.kerf + 1e-9 else 0.0
                ky = self.kerf if rem_h > self.kerf + 1e-9 else 0.0
                pw = w + kx
                ph = h + ky
                if pw <= 0 or ph <= 0:
                    continue
                if pw > fr.width or ph > fr.height:
                    continue
                anchors = [
                    ("top_left", fr.x, fr.y),
                    ("top_right", fr.x + fr.width - pw, fr.y),
                    ("bottom_left", fr.x, fr.y + fr.height - ph),
                    ("bottom_right", fr.x + fr.width - pw, fr.y + fr.height - ph),
                ]

                for anchor_name, px, py in anchors:
                    if px < fr.x - 1e-9 or py < fr.y - 1e-9:
                        continue
                    if px + pw > fr.x + fr.width + 1e-9 or py + ph > fr.y + fr.height + 1e-9:
                        continue
                    if px + pw > sheet_length or py + ph > sheet_width:
                        continue

                    if anchor_name == "top_left":
                        option_a = [
                            FreeRect(px + pw, py, fr.width - pw, ph),
                            FreeRect(fr.x, py + ph, fr.width, fr.height - ph),
                        ]
                        option_b = [
                            FreeRect(px + pw, fr.y, fr.width - pw, fr.height),
                            FreeRect(fr.x, py + ph, pw, fr.height - ph),
                        ]
                    elif anchor_name == "top_right":
                        option_a = [
                            FreeRect(fr.x, py, fr.width - pw, ph),
                            FreeRect(fr.x, py + ph, fr.width, fr.height - ph),
                        ]
                        option_b = [
                            FreeRect(fr.x, fr.y, fr.width - pw, fr.height),
                            FreeRect(px, py + ph, pw, fr.height - ph),
                        ]
                    elif anchor_name == "bottom_left":
                        option_a = [
                            FreeRect(px + pw, py, fr.width - pw, ph),
                            FreeRect(fr.x, fr.y, fr.width, fr.height - ph),
                        ]
                        option_b = [
                            FreeRect(px + pw, fr.y, fr.width - pw, fr.height),
                            FreeRect(fr.x, fr.y, pw, fr.height - ph),
                        ]
                    else:  # bottom_right
                        option_a = [
                            FreeRect(fr.x, py, fr.width - pw, ph),
                            FreeRect(fr.x, fr.y, fr.width, fr.height - ph),
                        ]
                        option_b = [
                            FreeRect(fr.x, fr.y, fr.width - pw, fr.height),
                            FreeRect(px, fr.y, pw, fr.height - ph),
                        ]

                    opt1 = [r for r in option_a if r.width > 0 and r.height > 0]
                    opt2 = [r for r in option_b if r.width > 0 and r.height > 0]
                    others = [r for j, r in enumerate(free_rects) if j != fr_idx]
                    new_free1 = others + opt1
                    new_free2 = others + opt2
                    score1 = _leftover_score(new_free1)
                    score2 = _leftover_score(new_free2)
                    chosen_free = new_free1 if score1 >= score2 else new_free2

                    metrics = _compute_offcut_metrics_from_rects(
                        chosen_free,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    candidate_placements = existing_placements + [
                        Rect(
                            x=px,
                            y=py,
                            width=w,
                            height=h,
                            part_id=str(part["id"]),
                            part_name=str(part["name"]),
                            rotated=bool(cand.get("rotated", False)),
                        )
                    ]
                    compactness = _compute_placement_compactness_metrics(candidate_placements)
                    cohesion = _compute_layout_cohesion_metrics(
                        candidate_placements,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    support = _compute_support_gap_metrics(
                        candidate_placements,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    workflow = _compute_cut_workflow_metrics(candidate_placements)
                    candidate_best = (
                        max(0.0, fr.area - (pw * ph)),
                        float(metrics["total_offcut_area"]),
                        float(compactness["bbox_void_area"]),
                        float(compactness["bbox_area"]),
                        float(support["unsupported_below_area"]),
                        float(support["unsupported_right_area"]),
                        float(support["max_drop_gap"]),
                        float(workflow["singleton_strip_area"]),
                        float(workflow["strip_switch_penalty"]),
                        float(metrics["interior_void_area"]),
                        -float(cohesion["adjacency_contact"]),
                        -float(cohesion["edge_contact"]),
                        -float(workflow["dominant_strip_area"]),
                        -float(metrics["reusable_offcut_area"]),
                        -float(metrics["offcut_squareness_score"]),
                        float(metrics["largest_offcut_area"]),
                        float(metrics["main_offcut_ratio"]),
                        float(metrics["skinny_scrap_area"]),
                        float(metrics["offcut_fragment_count"]),
                        float(cand.get("rotated", False)),
                    )
                    if best is None or candidate_best < best:
                        best = candidate_best
                        best_new_free = self._prune_free_rects(chosen_free)
                        placement = Rect(
                            x=px,
                            y=py,
                            width=w,
                            height=h,
                            part_id=str(part["id"]),
                            part_name=str(part["name"]),
                            rotated=bool(cand.get("rotated", False)),
                        )
                        best_placement = placement
                        best_meta = {
                            "host_free_rect": FreeRect(fr.x, fr.y, fr.width, fr.height),
                            "anchor": anchor_name,
                        }

        if best is None:
            return None, free_rects, None
        return best_placement, best_new_free or free_rects, best_meta

    def _compute_leftovers(
        self,
        placements: list[Rect],
        sheet_length: float,
        sheet_width: float,
    ) -> list[FreeRect]:
        free_rects: list[FreeRect] = [FreeRect(0.0, 0.0, float(sheet_length), float(sheet_width))]

        def _intersects(free_rect: FreeRect, rect: Rect) -> bool:
            return not (
                (float(rect.x) + float(rect.width)) <= free_rect.x + 1e-9
                or float(rect.x) >= (free_rect.x + free_rect.width) - 1e-9
                or (float(rect.y) + float(rect.height)) <= free_rect.y + 1e-9
                or float(rect.y) >= (free_rect.y + free_rect.height) - 1e-9
            )

        def _split(free_rect: FreeRect, rect: Rect) -> list[FreeRect]:
            left = float(rect.x)
            top = float(rect.y)
            right = left + float(rect.width)
            bottom = top + float(rect.height)

            fr_right = free_rect.x + free_rect.width
            fr_bottom = free_rect.y + free_rect.height
            new_rects: list[FreeRect] = []

            if top > free_rect.y + 1e-9:
                new_rects.append(FreeRect(free_rect.x, free_rect.y, free_rect.width, top - free_rect.y))
            if bottom < fr_bottom - 1e-9:
                new_rects.append(FreeRect(free_rect.x, bottom, free_rect.width, fr_bottom - bottom))
            if left > free_rect.x + 1e-9:
                new_rects.append(FreeRect(free_rect.x, free_rect.y, left - free_rect.x, free_rect.height))
            if right < fr_right - 1e-9:
                new_rects.append(FreeRect(right, free_rect.y, fr_right - right, free_rect.height))

            return [r for r in new_rects if r.width > 1e-9 and r.height > 1e-9]

        for rect in sorted(placements, key=lambda item: (float(item.y), float(item.x), -float(item.width * item.height))):
            next_free: list[FreeRect] = []
            for free_rect in free_rects:
                if not _intersects(free_rect, rect):
                    next_free.append(free_rect)
                    continue
                next_free.extend(_split(free_rect, rect))
            free_rects = self._prune_free_rects(next_free)

        return self._prune_free_rects(free_rects)

    def _sheet_cleanup_score(
        self,
        placements: list[Rect] | list[dict[str, Any]],
        leftovers: list[FreeRect] | list[dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[float, ...]:
        metrics = _compute_offcut_metrics_from_rects(
            leftovers,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        compactness = _compute_placement_compactness_metrics(placements)
        cohesion = _compute_layout_cohesion_metrics(
            placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        support = _compute_support_gap_metrics(
            placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        workflow = _compute_cut_workflow_metrics(placements)
        total_waste = sum(_rect_area(r) for r in leftovers)
        alignment_penalty = sum((_rect_y(p) * 1.0) + (_rect_x(p) * 0.25) for p in placements)
        return (
            total_waste,
            *_offcut_quality_key(metrics),
            float(compactness["bbox_void_area"]),
            float(compactness["bbox_area"]),
            float(support["unsupported_below_area"]),
            float(support["unsupported_right_area"]),
            float(support["max_drop_gap"]),
            float(workflow["singleton_strip_area"]),
            float(workflow["strip_switch_penalty"]),
            -float(cohesion["adjacency_contact"]),
            -float(cohesion["edge_contact"]),
            -float(workflow["dominant_strip_area"]),
            alignment_penalty,
        )

    def _cleanup_sheet_layout(
        self,
        parts: list[dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[list[Rect], list[FreeRect]]:
        if len(parts) <= 1:
            placements, _, leftovers, _placement_trace = self.optimize_sheet(parts, sheet_length, sheet_width)
            return placements, leftovers

        original_mode = self.sort_mode
        cleanup_modes = []
        for mode in (self.sort_mode, "perimeter", "max_side", "area", "short_side"):
            if mode not in cleanup_modes:
                cleanup_modes.append(mode)

        best_layout: tuple[list[Rect], list[FreeRect]] | None = None
        best_score: tuple[float, ...] | None = None

        try:
            for mode in cleanup_modes:
                self.sort_mode = mode
                ordered_parts = sorted(parts, key=self._sort_key, reverse=True)
                placements, unplaced, leftovers, _placement_trace = self.optimize_sheet(ordered_parts, sheet_length, sheet_width)
                if unplaced:
                    continue
                score = self._sheet_cleanup_score(
                    placements,
                    leftovers,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_layout = (placements, leftovers)
        finally:
            self.sort_mode = original_mode

        if best_layout is None:
            ordered_parts = sorted(parts, key=self._sort_key, reverse=True)
            placements, _, leftovers, _placement_trace = self.optimize_sheet(ordered_parts, sheet_length, sheet_width)
            return placements, leftovers
        return best_layout

    def _nudge_sheet_layout(
        self,
        placements: list[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[list[Rect], list[FreeRect]]:
        if len(placements) <= 1:
            return placements, self._compute_leftovers(placements, sheet_length, sheet_width)

        current = [
            Rect(
                x=float(p.x),
                y=float(p.y),
                width=float(p.width),
                height=float(p.height),
                part_id=str(p.part_id),
                part_name=str(p.part_name),
                rotated=bool(p.rotated),
            )
            for p in placements
        ]

        def _candidate_positions(rect: Rect, others: list[Rect]) -> tuple[list[float], list[float]]:
            xs = {0.0, max(0.0, float(sheet_length) - float(rect.width))}
            ys = {0.0, max(0.0, float(sheet_width) - float(rect.height))}
            for other in others:
                xs.add(max(0.0, float(other.x + other.width)))
                xs.add(max(0.0, float(other.x - rect.width)))
                ys.add(max(0.0, float(other.y + other.height)))
                ys.add(max(0.0, float(other.y - rect.height)))
            valid_x = sorted({round(x, 6) for x in xs if 0.0 <= x <= float(sheet_length) - float(rect.width) + 1e-6})
            valid_y = sorted({round(y, 6) for y in ys if 0.0 <= y <= float(sheet_width) - float(rect.height) + 1e-6})
            return valid_x, valid_y

        improved = True
        while improved:
            improved = False
            current_leftovers = self._compute_leftovers(current, sheet_length, sheet_width)
            current_score = self._sheet_cleanup_score(
                current,
                current_leftovers,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            for idx, rect in enumerate(list(current)):
                others = [r for j, r in enumerate(current) if j != idx]
                valid_x, valid_y = _candidate_positions(rect, others)
                best_rect = rect
                best_score = current_score
                for x in valid_x:
                    for y in valid_y:
                        if abs(x - rect.x) <= 1e-6 and abs(y - rect.y) <= 1e-6:
                            continue
                        candidate = Rect(
                            x=float(x),
                            y=float(y),
                            width=float(rect.width),
                            height=float(rect.height),
                            part_id=str(rect.part_id),
                            part_name=str(rect.part_name),
                            rotated=bool(rect.rotated),
                        )
                        if not _non_overlapping(candidate, others):
                            continue
                        candidate_placements = others + [candidate]
                        candidate_leftovers = self._compute_leftovers(candidate_placements, sheet_length, sheet_width)
                        score = self._sheet_cleanup_score(
                            candidate_placements,
                            candidate_leftovers,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        )
                        if score < best_score:
                            best_score = score
                            best_rect = candidate
                if best_rect is not rect:
                    current[idx] = best_rect
                    improved = True
                    break

        final_leftovers = self._compute_leftovers(current, sheet_length, sheet_width)
        return current, final_leftovers

    def _gravity_compact_sheet_layout(
        self,
        placements: list[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[list[Rect], list[FreeRect]]:
        if len(placements) <= 1:
            return placements, self._compute_leftovers(placements, sheet_length, sheet_width)

        current = [
            Rect(
                x=float(p.x),
                y=float(p.y),
                width=float(p.width),
                height=float(p.height),
                part_id=str(p.part_id),
                part_name=str(p.part_name),
                rotated=bool(p.rotated),
            )
            for p in placements
        ]

        improved = True
        eps = 1e-6
        while improved:
            improved = False
            ordered = sorted(range(len(current)), key=lambda idx: (current[idx].y, current[idx].x, -(current[idx].width * current[idx].height)))
            for idx in ordered:
                rect = current[idx]
                others = [r for j, r in enumerate(current) if j != idx]

                support_y = 0.0
                for other in others:
                    overlap_x = min(rect.x + rect.width, other.x + other.width) - max(rect.x, other.x)
                    if overlap_x <= eps:
                        continue
                    if (other.y + other.height) <= rect.y + eps:
                        support_y = max(support_y, other.y + other.height)
                if support_y < rect.y - eps:
                    candidate = Rect(
                        x=float(rect.x),
                        y=float(support_y),
                        width=float(rect.width),
                        height=float(rect.height),
                        part_id=str(rect.part_id),
                        part_name=str(rect.part_name),
                        rotated=bool(rect.rotated),
                    )
                    if _non_overlapping(candidate, others):
                        current[idx] = candidate
                        rect = candidate
                        improved = True

                support_x = 0.0
                for other in others:
                    overlap_y = min(rect.y + rect.height, other.y + other.height) - max(rect.y, other.y)
                    if overlap_y <= eps:
                        continue
                    if (other.x + other.width) <= rect.x + eps:
                        support_x = max(support_x, other.x + other.width)
                if support_x < rect.x - eps:
                    candidate = Rect(
                        x=float(support_x),
                        y=float(rect.y),
                        width=float(rect.width),
                        height=float(rect.height),
                        part_id=str(rect.part_id),
                        part_name=str(rect.part_name),
                        rotated=bool(rect.rotated),
                    )
                    if _non_overlapping(candidate, others):
                        current[idx] = candidate
                        improved = True

        final_leftovers = self._compute_leftovers(current, sheet_length, sheet_width)
        return current, final_leftovers

    def _tail_sheet_score(
        self,
        placements: list[Rect] | list[dict[str, Any]],
        leftovers: list[FreeRect] | list[dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[float, ...]:
        metrics = _compute_offcut_metrics_from_rects(
            leftovers,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        return (
            float(len(placements)),
            *_offcut_quality_key(metrics),
            float(metrics.get("skinny_scrap_count") or 0.0),
            float(metrics.get("reusable_offcut_count") or 0.0) * -1.0,
        )

    def optimize_sheet(
        self,
        parts: List[Dict[str, Any]],
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[List[Rect], List[Dict], List[FreeRect], List[Dict[str, Any]]]:
        placements: list[Rect] = []
        remaining: list[Dict] = []
        free_rects: list[FreeRect] = [FreeRect(0.0, 0.0, sheet_length, sheet_width)]
        placement_trace: list[dict[str, Any]] = []

        for part in parts:
            placement, new_free, placement_meta = self._place_in_sheet(
                free_rects,
                part,
                sheet_length,
                sheet_width,
                existing_placements=placements,
            )
            if placement is None:
                remaining.append(part)
                continue
            placements.append(placement)
            free_rects = new_free
            if placement_meta and placement_meta.get("host_free_rect") is not None:
                placement_trace.append(
                    {
                        "host_free_rect": _cut_plan_rect_payload(placement_meta["host_free_rect"]),
                        "anchor": str(placement_meta.get("anchor") or "bottom_left"),
                    }
                )

        return placements, remaining, free_rects, placement_trace

    def optimize(
        self,
        parts: List[Any],
        boards: List[Any],
        sheet_length: float,
        sheet_width: float,
        max_sheets: int = None,
    ) -> Dict[str, Any]:
        max_sheets = int(max_sheets or self.max_sheets)

        if len(parts) > config.optimization.max_parts:
            raise ValueError(f"Too many parts: {len(parts)}. Maximum: {config.optimization.max_parts}")

        expanded_parts: list[dict[str, Any]] = []
        for part in parts:
            qty = int(getattr(part, "quantity", 1) or 1)
            candidates = getattr(part, "candidates", None)
            if candidates is None:
                candidates = [{"width": float(getattr(part, "length_mm", 0)), "height": float(getattr(part, "width_mm", 0)), "rotated": False}]
            for i in range(qty):
                expanded_parts.append(
                    {
                        "id": str(getattr(part, "id", i)) + (f":{i}" if qty > 1 else ""),
                        "name": getattr(part, "name", "Part") or "Part",
                        "candidates": candidates,
                        "original": part,
                    }
                )

        expanded_parts.sort(key=self._sort_key, reverse=True)
        total_parts_area = sum(
            max((float(c["width"]) * float(c["height"]) for c in (p.get("candidates") or [])), default=0.0) for p in expanded_parts
        )

        all_sheets: list[Sheet] = []
        all_leftovers: list[list[FreeRect]] = []
        remaining = expanded_parts
        sheet_id = 0

        while remaining and sheet_id < max_sheets:
            sheet_id += 1
            placements, unplaced, leftovers, _placement_trace = self.optimize_sheet(remaining, sheet_length, sheet_width)
            if placements:
                used_area = sum(p.width * p.height for p in placements)
                sheet = Sheet(id=sheet_id, placements=placements, used_area=used_area, total_area=sheet_length * sheet_width)
                all_sheets.append(sheet)
                all_leftovers.append(leftovers)
            remaining = unplaced
            if not remaining:
                break

        # Tail-sheet consolidation pass:
        # repeatedly try to absorb low-utilization trailing sheets into earlier sheets without
        # disturbing already good layouts. This targets the common "last one or two sheets are
        # obviously mergeable" cases while keeping runtime bounded.
        if len(all_sheets) >= 2:
            part_by_id: dict[str, dict[str, Any]] = {str(p["id"]): p for p in expanded_parts}

            def _copy_free(rects: list[FreeRect]) -> list[FreeRect]:
                return [FreeRect(r.x, r.y, r.width, r.height) for r in rects]

            def _part_area(pid: str) -> float:
                return max(
                    (float(c["width"]) * float(c["height"]) for c in (part_by_id.get(pid, {}).get("candidates") or [])),
                    default=0.0,
                )

            def _can_try_merge(sheet: Sheet) -> bool:
                # Keep the pass predictable. We only try to absorb clearly weak tail sheets:
                # few parts, or very low utilization, or very small used area.
                placement_count = len(sheet.placements)
                if placement_count == 0:
                    return False
                if placement_count <= 14:
                    return True
                if sheet.utilization <= 55.0:
                    return True
                if sheet.used_area <= sheet.total_area * 0.42:
                    return True
                return False

            progress = True
            while progress and len(all_sheets) >= 2:
                progress = False

                # Scan from the end backwards so we preferentially compress the weakest tail sheets.
                for candidate_index in range(len(all_sheets) - 1, 0, -1):
                    candidate_sheet = all_sheets[candidate_index]
                    if not _can_try_merge(candidate_sheet):
                        continue

                    candidate_ids = [str(p.part_id) for p in candidate_sheet.placements]
                    if not candidate_ids:
                        continue

                    candidate_ids.sort(key=_part_area, reverse=True)

                    scratch_free = [_copy_free(r) for r in all_leftovers]
                    scratch_additions: list[list[Rect]] = [[] for _ in all_sheets]
                    can_move_all = True

                    for pid in candidate_ids:
                        part = part_by_id.get(pid)
                        if not part:
                            can_move_all = False
                            break

                        placed = False
                        for sheet_index in range(len(all_sheets)):
                            if sheet_index == candidate_index:
                                continue
                            placement, new_free, _placement_meta = self._place_in_sheet(
                                scratch_free[sheet_index],
                                part,
                                sheet_length,
                                sheet_width,
                                existing_placements=all_sheets[sheet_index].placements + scratch_additions[sheet_index],
                            )
                            if placement is None:
                                continue
                            scratch_free[sheet_index] = new_free
                            scratch_additions[sheet_index].append(placement)
                            placed = True
                            break

                        if not placed:
                            can_move_all = False
                            break

                    if not can_move_all:
                        continue

                    for sheet_index in range(len(all_sheets)):
                        if sheet_index == candidate_index:
                            continue
                        if scratch_additions[sheet_index]:
                            all_sheets[sheet_index].placements.extend(scratch_additions[sheet_index])
                            all_sheets[sheet_index].used_area += sum(p.width * p.height for p in scratch_additions[sheet_index])
                            all_leftovers[sheet_index] = scratch_free[sheet_index]

                    all_sheets.pop(candidate_index)
                    all_leftovers.pop(candidate_index)
                    progress = True
                    break

            for idx, sheet in enumerate(all_sheets, start=1):
                sheet.id = idx

        # Local cleanup pass:
        # Once sheet count is settled, repack each individual sheet with stronger
        # offcut-quality scoring. This does not change sheet count, only sheet-internal
        # placement quality.
        if all_sheets:
            part_by_id = {str(p["id"]): p for p in expanded_parts}
            for idx, sheet in enumerate(all_sheets):
                sheet_parts = [part_by_id.get(str(p.part_id)) for p in sheet.placements]
                sheet_parts = [p for p in sheet_parts if p]
                if not sheet_parts:
                    continue
                new_placements, new_leftovers = self._cleanup_sheet_layout(
                    sheet_parts,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if len(new_placements) != len(sheet_parts):
                    continue
                old_score = self._sheet_cleanup_score(
                    sheet.placements,
                    all_leftovers[idx] if idx < len(all_leftovers) else [],
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                new_score = self._sheet_cleanup_score(
                    new_placements,
                    new_leftovers,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if new_score < old_score:
                    sheet.placements = new_placements
                    sheet.used_area = sum(p.width * p.height for p in new_placements)
                    all_leftovers[idx] = new_leftovers
                nudged_placements, nudged_leftovers = self._nudge_sheet_layout(
                    sheet.placements,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                nudged_score = self._sheet_cleanup_score(
                    nudged_placements,
                    nudged_leftovers,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                current_score = self._sheet_cleanup_score(
                    sheet.placements,
                    all_leftovers[idx] if idx < len(all_leftovers) else [],
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if nudged_score < current_score:
                    sheet.placements = nudged_placements
                    sheet.used_area = sum(p.width * p.height for p in nudged_placements)
                    all_leftovers[idx] = nudged_leftovers
                gravity_placements, gravity_leftovers = self._gravity_compact_sheet_layout(
                    sheet.placements,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                gravity_score = self._sheet_cleanup_score(
                    gravity_placements,
                    gravity_leftovers,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                current_score = self._sheet_cleanup_score(
                    sheet.placements,
                    all_leftovers[idx] if idx < len(all_leftovers) else [],
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if gravity_score < current_score:
                    sheet.placements = gravity_placements
                    sheet.used_area = sum(p.width * p.height for p in gravity_placements)
                    all_leftovers[idx] = gravity_leftovers
                is_tail_sheet = idx == (len(all_sheets) - 1)
                if is_tail_sheet and 1 < len(sheet_parts) <= 8:
                    shelf_opt = ShelfAlgorithm(kerf_mm=self.kerf)
                    shelf_placements, shelf_unplaced = shelf_opt.optimize_sheet(
                        sheet_parts,
                        sheet_length,
                        sheet_width,
                    )
                    if not shelf_unplaced and shelf_placements:
                        shelf_leftovers = _compute_leftovers_from_placements(
                            shelf_placements,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        )
                        shelf_tail_score = self._tail_sheet_score(
                            shelf_placements,
                            shelf_leftovers,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        )
                        current_tail_score = self._tail_sheet_score(
                            sheet.placements,
                            all_leftovers[idx] if idx < len(all_leftovers) else [],
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        )
                        if shelf_tail_score < current_tail_score:
                            sheet.placements = shelf_placements
                            sheet.used_area = sum(p.width * p.height for p in shelf_placements)
                            all_leftovers[idx] = [
                                FreeRect(
                                    float(r.get("x") or 0.0),
                                    float(r.get("y") or 0.0),
                                    float(r.get("width") or 0.0),
                                    float(r.get("height") or 0.0),
                                )
                                for r in shelf_leftovers
                                if float(r.get("width") or 0.0) > 1e-9 and float(r.get("height") or 0.0) > 1e-9
                            ]

        if all_sheets:
            ranked_sheets = []
            for idx, sheet in enumerate(all_sheets):
                leftovers = all_leftovers[idx] if idx < len(all_leftovers) else []
                metrics = _compute_offcut_metrics_from_rects(
                    leftovers,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                ranked_sheets.append(
                    (
                        -float(sheet.used_area),
                        -float(sheet.utilization),
                        float(metrics.get("reusable_offcut_area") or 0.0),
                        float(metrics.get("edge_hugging_area") or 0.0),
                        float(metrics.get("offcut_fragment_count") or 0.0),
                        float(metrics.get("largest_offcut_area") or 0.0),
                        float(metrics.get("main_offcut_ratio") or 0.0),
                        idx,
                    )
                )
            ordering = [item[-1] for item in sorted(ranked_sheets)]
            all_sheets = [all_sheets[i] for i in ordering]
            all_leftovers = [all_leftovers[i] for i in ordering]
            for idx, sheet in enumerate(all_sheets, start=1):
                sheet.id = idx

        total_used = sum(s.used_area for s in all_sheets)
        total_available = sum(s.total_area for s in all_sheets)
        overall_efficiency = (total_used / total_available * 100) if total_available > 0 else 0

        sheets_payload = []
        for idx, s in enumerate(all_sheets):
            sheet_leftovers = [
                {"x": r.x, "y": r.y, "width": r.width, "height": r.height, "area": r.area}
                for r in (all_leftovers[idx] if idx < len(all_leftovers) else [])
                if r.width > 0 and r.height > 0 and r.area > 0
            ]
            sheets_payload.append(
                {
                    "id": s.id,
                    "placements": [
                        {
                            "part_id": p.part_id,
                            "part_name": p.part_name,
                            "x": p.x,
                            "y": p.y,
                            "width": p.width,
                            "height": p.height,
                            "rotated": p.rotated,
                        }
                        for p in s.placements
                    ],
                    "utilization": round(s.utilization, 2),
                    "used_area": s.used_area,
                    "total_area": s.total_area,
                    "leftovers": sheet_leftovers,
                    "offcut_metrics": _compute_offcut_metrics_from_rects(
                        sheet_leftovers,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    ),
                }
            )

        offcut_metrics = _merge_offcut_metrics(
            [
                _compute_offcut_metrics_from_rects(
                    s.get("leftovers") or [],
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                for s in sheets_payload
            ]
        )

        return {
            "sheets": sheets_payload,
            "sheets_used": len(all_sheets),
            "sheets_available": max_sheets,
            "unplaced_count": len(remaining),
            "efficiency_percent": round(overall_efficiency, 2),
            "total_parts_area": total_parts_area,
            "total_used_area": total_used,
            "kerf_mm": self.kerf,
            "algorithm": "guillotine",
            "offcut_metrics": offcut_metrics,
        }
