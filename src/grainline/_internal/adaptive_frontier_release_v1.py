# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""
Adaptive frontier release v1.

Design goals:
- do not pre-allocate zones / lanes / families
- let structure emerge from already placed parts and current free space
- keep early sheets dense by preferring placements that hug edges / existing mass
- allow large and small parts to compete at each step based on current fit, not a fixed template
"""

from __future__ import annotations

from bisect import bisect_left
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, cast

from grainline._internal.runtime import append_optimize_trace
from grainline._internal.placement_affinity import (
    attachment_score,
)
from grainline._internal.sheet_partition_search import PlannedSheet, find_sheet_plans
from grainline._internal.cut_plan_validation import validate_cut_plan


EPS = 1e-6
# Budget for the fixed-split fallback that runs only when the sheet-by-sheet search misses a lower sheet count.
SHEET_PLAN_SEARCH_BUDGET_S = 0.4
SHEET_PLAN_SEARCH_MAX_TRIES = 6000
SHEET_PLAN_MAX_PLANS = 3


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float
    part_id: str
    part_name: str
    rotated: bool = False
    area: float = field(init=False)

    def __post_init__(self) -> None:
        self.area = self.width * self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height


@dataclass
class FreeRect:
    x: float
    y: float
    width: float
    height: float
    area: float = field(init=False)

    def __post_init__(self) -> None:
        self.area = self.width * self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height


@dataclass
class SheetResult:
    id: int
    placements: List[Rect]
    free_rects: List[FreeRect]
    sheet_length: float
    sheet_width: float
    debug: Dict[str, Any] = field(default_factory=dict)

    @property
    def used_area(self) -> float:
        return sum(rect.area for rect in self.placements)

    @property
    def total_area(self) -> float:
        return self.sheet_length * self.sheet_width

    @property
    def utilization(self) -> float:
        return (self.used_area / self.total_area * 100.0) if self.total_area > 0 else 0.0


@dataclass
class ChooseNextStateView:
    algorithm: "AdaptiveFrontierBuildV1Algorithm"
    remaining: Sequence[Dict[str, Any]]
    placements: Sequence[Rect]
    free_rects: Sequence[FreeRect]
    sheet_length: float
    sheet_width: float
    _host_stats_by_part_id: Dict[str, Dict[str, float]] = field(default_factory=dict)
    _placements_sorted_by_x: Optional[List[Rect]] = None
    _placements_sorted_by_x_keys: Optional[List[float]] = None
    _cluster_bbox: Optional[Tuple[float, float, float, float]] = None
    _placements_signature: Optional[Tuple[Tuple[float, float, float, float, bool], ...]] = None

    def get_part_candidates(self, part: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.algorithm._get_candidates(part)

    def get_host_stats(self, part: Dict[str, Any]) -> Dict[str, float]:
        part_id = str(part.get("id") or "")
        cached = self._host_stats_by_part_id.get(part_id)
        if cached is not None:
            self.algorithm._profile_add("state_view_host_hits", 0.0)
            return cached
        self.algorithm._profile_add("state_view_host_misses", 0.0)
        started = time.perf_counter()
        result = self.algorithm._fit_host_stats(part, self.free_rects)
        self.algorithm._profile_add("fit_host_stats", (time.perf_counter() - started) * 1000.0)
        self._host_stats_by_part_id[part_id] = result
        return result

    def get_overlap_candidates(self, rect: Rect) -> Sequence[Rect]:
        if not self.placements:
            return ()
        if self._placements_sorted_by_x is None or self._placements_sorted_by_x_keys is None:
            sorted_placements = sorted(self.placements, key=lambda item: item.x)
            self._placements_sorted_by_x = sorted_placements
            self._placements_sorted_by_x_keys = [item.x for item in sorted_placements]
        cutoff = bisect_left(self._placements_sorted_by_x_keys, rect.right - EPS)
        candidates = [other for other in self._placements_sorted_by_x[:cutoff] if other.right > rect.x + EPS]
        if len(candidates) < len(self.placements):
            self.algorithm._profile_add("state_view_overlap_hits", 0.0)
        return candidates

    def get_cluster_bbox(self) -> Tuple[float, float, float, float] | None:
        if self._cluster_bbox is not None:
            return self._cluster_bbox
        if not self.placements:
            return None
        min_x = min(item.x for item in self.placements)
        min_y = min(item.y for item in self.placements)
        max_x = max(item.right for item in self.placements)
        max_y = max(item.top for item in self.placements)
        self._cluster_bbox = (min_x, min_y, max_x, max_y)
        return self._cluster_bbox

    def get_placements_signature(self) -> Tuple[Tuple[float, float, float, float, bool], ...]:
        if self._placements_signature is not None:
            return self._placements_signature
        signature = tuple(
            sorted(
                (
                    round(item.x, 4),
                    round(item.y, 4),
                    round(item.width, 4),
                    round(item.height, 4),
                    bool(item.rotated),
                )
                for item in self.placements
            )
        )
        self._placements_signature = signature
        return signature


def _rect_payload(rect: Any) -> Dict[str, float]:
    if isinstance(rect, dict):
        x = float(rect.get("x", 0.0) or 0.0)
        y = float(rect.get("y", 0.0) or 0.0)
        width = float(rect.get("width", 0.0) or 0.0)
        height = float(rect.get("height", 0.0) or 0.0)
    else:
        x = float(getattr(rect, "x", 0.0) or 0.0)
        y = float(getattr(rect, "y", 0.0) or 0.0)
        width = float(getattr(rect, "width", 0.0) or 0.0)
        height = float(getattr(rect, "height", 0.0) or 0.0)
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


class CutPlanRecorder:
    def __init__(self, *, kerf_mm: float, sheet_id: int, source_strategy: str) -> None:
        self.kerf_mm = max(0.0, float(kerf_mm or 0.0))
        self.sheet_id = int(sheet_id)
        self.source_strategy = str(source_strategy or "unknown")
        self.steps: List[Dict[str, Any]] = []
        self._step_no = 1
        self._workpiece_no = 1
        self._workpiece_ids_by_key: Dict[Tuple[float, float, float, float], str] = {}
        self._producer_step_by_workpiece_id: Dict[str, str] = {}

    def _workpiece_key(self, rect: Dict[str, float]) -> Tuple[float, float, float, float]:
        return (
            round(float(rect["x"]), 4),
            round(float(rect["y"]), 4),
            round(float(rect["width"]), 4),
            round(float(rect["height"]), 4),
        )

    def _get_or_create_workpiece_id(self, rect: Dict[str, float]) -> str:
        key = self._workpiece_key(rect)
        existing = self._workpiece_ids_by_key.get(key)
        if existing:
            return existing
        workpiece_id = f"sheet-{self.sheet_id}-{self.source_strategy}-wp-{self._workpiece_no}"
        self._workpiece_no += 1
        self._workpiece_ids_by_key[key] = workpiece_id
        return workpiece_id

    def _line_from_band(self, axis: str, band: Dict[str, float]) -> tuple[Dict[str, float], Dict[str, float]]:
        if axis == "horizontal":
            y = float(band["y"]) + float(band["height"]) * 0.5
            return (
                {"x": float(band["x"]), "y": y},
                {"x": float(band["x"]) + float(band["width"]), "y": y},
            )
        x = float(band["x"]) + float(band["width"]) * 0.5
        return (
            {"x": x, "y": float(band["y"])},
            {"x": x, "y": float(band["y"]) + float(band["height"])},
        )

    def _entry_side(self, axis: str, anchor: str) -> str:
        if axis == "horizontal":
            return "right" if anchor in {"br", "tr"} else "left"
        return "top" if anchor in {"tl", "tr"} else "bottom"

    def _append_step(
        self,
        *,
        axis: str,
        cut_band: Dict[str, float],
        workpiece_before: Dict[str, float],
        workpieces_after: List[Dict[str, float]],
        target_part_ids: List[str],
        target_part_names: List[str],
        anchor: str,
        unlocks_part_ids: List[str] | None = None,
    ) -> None:
        start, end = self._line_from_band(axis, cut_band)
        action = "rip" if axis == "horizontal" else "crosscut"
        label = (
            f"{'Rip' if axis == 'horizontal' else 'Crosscut'} {target_part_names[0]}"
            if len(target_part_names) == 1
            else ("Rip cut" if axis == "horizontal" else "Crosscut")
        )
        workpiece_before_id = self._get_or_create_workpiece_id(workpiece_before)
        depends_on_step_ids: List[str] = []
        producer = self._producer_step_by_workpiece_id.get(workpiece_before_id)
        if producer:
            depends_on_step_ids.append(producer)
        workpiece_after_ids: List[str] = []
        step_id = f"sheet-{self.sheet_id}-{self.source_strategy}-{self._step_no}"
        for item in workpieces_after:
            workpiece_id = self._get_or_create_workpiece_id(item)
            workpiece_after_ids.append(workpiece_id)
            self._producer_step_by_workpiece_id[workpiece_id] = step_id
        self.steps.append(
            {
                "id": step_id,
                "step_number": int(self._step_no),
                "action": action,
                "axis": axis,
                "label": label,
                "sheet": int(self.sheet_id),
                "kerf_mm": float(self.kerf_mm),
                "start": start,
                "end": end,
                "cut_band": dict(cut_band),
                "workpiece_before": dict(workpiece_before),
                "workpieces_after": [dict(item) for item in workpieces_after],
                "workpiece_before_id": workpiece_before_id,
                "workpiece_after_ids": workpiece_after_ids,
                "depends_on_step_ids": depends_on_step_ids,
                "through_cut": True,
                "entry_side": self._entry_side(axis, anchor),
                "anchor": str(anchor),
                "target_part_ids": list(target_part_ids),
                "target_part_names": list(target_part_names),
                "unlocks_part_ids": list(unlocks_part_ids or []),
                "source_strategy": self.source_strategy,
            }
        )
        self._step_no += 1

    def record_part_isolation(self, *, part_rect: Rect, host_free_rect: Any, anchor: str) -> None:
        host = _rect_payload(host_free_rect)
        part = _rect_payload(part_rect)
        target_part_ids = [str(part_rect.part_id)]
        target_part_names = [str(part_rect.part_name)]
        current_piece = dict(host)

        if anchor in {"bl", "br"}:
            cut_y = float(part["y"]) + float(part["height"]) + self.kerf_mm
            if cut_y < float(host["y"]) + float(host["height"]) - EPS:
                cut_band = {
                    "x": float(host["x"]),
                    "y": float(part["y"]) + float(part["height"]),
                    "width": float(host["width"]),
                    "height": float(self.kerf_mm),
                }
                containing_piece = {
                    "x": float(host["x"]),
                    "y": float(host["y"]),
                    "width": float(host["width"]),
                    "height": float(part["height"]),
                }
                sibling_piece = {
                    "x": float(host["x"]),
                    "y": cut_y,
                    "width": float(host["width"]),
                    "height": float(host["y"]) + float(host["height"]) - cut_y,
                }
                self._append_step(
                    axis="horizontal",
                    cut_band=cut_band,
                    workpiece_before=host,
                    workpieces_after=[containing_piece, sibling_piece],
                    target_part_ids=target_part_ids,
                    target_part_names=target_part_names,
                    anchor=anchor,
                    unlocks_part_ids=[],
                )
                current_piece = containing_piece
        else:
            band_y = float(part["y"]) - self.kerf_mm
            if float(host["y"]) < band_y - EPS:
                cut_band = {
                    "x": float(host["x"]),
                    "y": band_y,
                    "width": float(host["width"]),
                    "height": float(self.kerf_mm),
                }
                sibling_piece = {
                    "x": float(host["x"]),
                    "y": float(host["y"]),
                    "width": float(host["width"]),
                    "height": band_y - float(host["y"]),
                }
                containing_piece = {
                    "x": float(host["x"]),
                    "y": float(part["y"]),
                    "width": float(host["width"]),
                    "height": float(part["height"]),
                }
                self._append_step(
                    axis="horizontal",
                    cut_band=cut_band,
                    workpiece_before=host,
                    workpieces_after=[sibling_piece, containing_piece],
                    target_part_ids=target_part_ids,
                    target_part_names=target_part_names,
                    anchor=anchor,
                    unlocks_part_ids=[],
                )
                current_piece = containing_piece

        if anchor in {"bl", "tl"}:
            cut_x = float(part["x"]) + float(part["width"]) + self.kerf_mm
            if cut_x < float(current_piece["x"]) + float(current_piece["width"]) - EPS:
                cut_band = {
                    "x": float(part["x"]) + float(part["width"]),
                    "y": float(current_piece["y"]),
                    "width": float(self.kerf_mm),
                    "height": float(current_piece["height"]),
                }
                part_piece = {
                    "x": float(part["x"]),
                    "y": float(current_piece["y"]),
                    "width": float(part["width"]),
                    "height": float(current_piece["height"]),
                }
                sibling_piece = {
                    "x": cut_x,
                    "y": float(current_piece["y"]),
                    "width": float(current_piece["x"]) + float(current_piece["width"]) - cut_x,
                    "height": float(current_piece["height"]),
                }
                self._append_step(
                    axis="vertical",
                    cut_band=cut_band,
                    workpiece_before=current_piece,
                    workpieces_after=[part_piece, sibling_piece],
                    target_part_ids=target_part_ids,
                    target_part_names=target_part_names,
                    anchor=anchor,
                    unlocks_part_ids=target_part_ids,
                )
        else:
            band_x = float(part["x"]) - self.kerf_mm
            if float(current_piece["x"]) < band_x - EPS:
                cut_band = {
                    "x": band_x,
                    "y": float(current_piece["y"]),
                    "width": float(self.kerf_mm),
                    "height": float(current_piece["height"]),
                }
                sibling_piece = {
                    "x": float(current_piece["x"]),
                    "y": float(current_piece["y"]),
                    "width": band_x - float(current_piece["x"]),
                    "height": float(current_piece["height"]),
                }
                part_piece = {
                    "x": float(part["x"]),
                    "y": float(current_piece["y"]),
                    "width": float(part["width"]),
                    "height": float(current_piece["height"]),
                }
                self._append_step(
                    axis="vertical",
                    cut_band=cut_band,
                    workpiece_before=current_piece,
                    workpieces_after=[sibling_piece, part_piece],
                    target_part_ids=target_part_ids,
                    target_part_names=target_part_names,
                    anchor=anchor,
                    unlocks_part_ids=target_part_ids,
                )

    def record_custom_cut(
        self,
        *,
        axis: str,
        cut_band: Dict[str, float],
        workpiece_before: Dict[str, float],
        workpieces_after: List[Dict[str, float]],
        target_part_ids: List[str],
        target_part_names: List[str],
        anchor: str,
        unlocks_part_ids: List[str] | None = None,
    ) -> None:
        self._append_step(
            axis=axis,
            cut_band=cut_band,
            workpiece_before=workpiece_before,
            workpieces_after=workpieces_after,
            target_part_ids=target_part_ids,
            target_part_names=target_part_names,
            anchor=anchor,
            unlocks_part_ids=unlocks_part_ids,
        )


def _build_isolation_cut_plan_steps(
    *,
    placements: Sequence[Rect],
    debug_steps: Sequence[Dict[str, Any]],
    kerf_mm: float,
    sheet_id: int,
    source_strategy: str,
) -> List[Dict[str, Any]]:
    recorder = CutPlanRecorder(kerf_mm=kerf_mm, sheet_id=sheet_id, source_strategy=source_strategy)
    placement_iter = iter(placements)
    for debug_step in debug_steps:
        if str(debug_step.get("action") or "") not in {"seed", "place", "cleanup-seed", "cleanup-place"}:
            continue
        host_free_rect = debug_step.get("host_free_rect")
        if not isinstance(host_free_rect, dict):
            continue
        try:
            placement = next(placement_iter)
        except StopIteration:
            break
        recorder.record_part_isolation(
            part_rect=placement,
            host_free_rect=host_free_rect,
            anchor=str(debug_step.get("anchor") or "bl"),
        )
    return recorder.steps


def _build_single_sheet_strip_cut_plan_steps(
    *,
    band_plan: Sequence[Dict[str, Any]],
    sheet_length: float,
    sheet_width: float,
    kerf_mm: float,
    sheet_id: int,
) -> List[Dict[str, Any]]:
    recorder = CutPlanRecorder(kerf_mm=kerf_mm, sheet_id=sheet_id, source_strategy="single-sheet-strip")
    remaining_x0 = 0.0
    remaining_x1 = float(sheet_length)
    for band in band_plan:
        rects = [item for item in (band.get("rects") or []) if isinstance(item, dict)]
        if not rects:
            continue
        band_x0 = float(band.get("band_x0") or 0.0)
        band_depth = float(band.get("band_depth") or 0.0)
        stack_from_left = bool(band.get("stack_from_left"))
        span_from_bottom = bool(band.get("span_from_bottom"))
        band_anchor = str(band.get("band_anchor") or ("bl" if span_from_bottom else "tl"))
        part_ids = [str(item) for item in (band.get("part_ids") or [])]
        part_names = [str(item) for item in (band.get("part_names") or [])]
        band_x1 = band_x0 + band_depth
        if stack_from_left:
            rip_cut = {"x": band_x1, "y": 0.0, "width": float(kerf_mm), "height": float(sheet_width)}
            left_piece = {"x": remaining_x0, "y": 0.0, "width": band_depth, "height": float(sheet_width)}
            right_piece = {
                "x": band_x1 + float(kerf_mm),
                "y": 0.0,
                "width": max(0.0, remaining_x1 - (band_x1 + float(kerf_mm))),
                "height": float(sheet_width),
            }
        else:
            rip_cut = {"x": band_x0 - float(kerf_mm), "y": 0.0, "width": float(kerf_mm), "height": float(sheet_width)}
            left_piece = {
                "x": remaining_x0,
                "y": 0.0,
                "width": max(0.0, (band_x0 - float(kerf_mm)) - remaining_x0),
                "height": float(sheet_width),
            }
            right_piece = {"x": band_x0, "y": 0.0, "width": band_depth, "height": float(sheet_width)}
        recorder.record_custom_cut(
            axis="vertical",
            cut_band=rip_cut,
            workpiece_before={"x": remaining_x0, "y": 0.0, "width": remaining_x1 - remaining_x0, "height": float(sheet_width)},
            workpieces_after=[left_piece, right_piece],
            target_part_ids=part_ids,
            target_part_names=part_names,
            anchor="bl" if stack_from_left else "br",
            unlocks_part_ids=[],
        )
        local_piece = {"x": band_x0, "y": 0.0, "width": band_depth, "height": float(sheet_width)}
        running_y = float(band.get("band_y0") or 0.0)
        for rect in rects[:-1]:
            cut_y = running_y + float(rect.get("height") or 0.0)
            cut_band = {"x": band_x0, "y": cut_y, "width": band_depth, "height": float(kerf_mm)}
            lower_piece = {
                "x": band_x0,
                "y": local_piece["y"],
                "width": band_depth,
                "height": cut_y - local_piece["y"],
            }
            upper_piece = {
                "x": band_x0,
                "y": cut_y + float(kerf_mm),
                "width": band_depth,
                "height": max(0.0, local_piece["y"] + local_piece["height"] - (cut_y + float(kerf_mm))),
            }
            recorder.record_custom_cut(
                axis="horizontal",
                cut_band=cut_band,
                workpiece_before=local_piece,
                workpieces_after=[lower_piece, upper_piece],
                target_part_ids=[str(rect.get("part_id") or "")],
                target_part_names=[str(rect.get("part_name") or "Part")],
                anchor=band_anchor,
                unlocks_part_ids=[str(rect.get("part_id") or "")],
            )
            local_piece = upper_piece if span_from_bottom else lower_piece
            running_y += float(rect.get("height") or 0.0) + float(kerf_mm)
        if stack_from_left:
            remaining_x0 = band_x1 + float(kerf_mm)
        else:
            remaining_x1 = band_x0 - float(kerf_mm)
    return recorder.steps


def _build_part_cut_paths(cut_plan_steps: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    step_by_id = {
        str(step.get("id") or ""): step
        for step in cut_plan_steps
        if isinstance(step, dict) and str(step.get("id") or "")
    }
    part_to_direct_steps: Dict[str, List[str]] = {}
    for step in cut_plan_steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "")
        if not step_id:
            continue
        seen_part_ids: set[str] = set()
        for raw_part_id in list(step.get("target_part_ids") or []) + list(step.get("unlocks_part_ids") or []):
            part_id = str(raw_part_id or "")
            if not part_id or part_id in seen_part_ids:
                continue
            seen_part_ids.add(part_id)
            part_to_direct_steps.setdefault(part_id, []).append(step_id)

    part_paths: Dict[str, Dict[str, Any]] = {}
    for part_id, direct_step_ids in part_to_direct_steps.items():
        visited_steps: set[str] = set()
        stack = list(direct_step_ids)
        while stack:
            step_id = stack.pop()
            if step_id in visited_steps:
                continue
            visited_steps.add(step_id)
            step = step_by_id.get(step_id) or {}
            for dependency_id in step.get("depends_on_step_ids") or []:
                dependency = str(dependency_id or "")
                if dependency and dependency not in visited_steps:
                    stack.append(dependency)
        ordered_step_ids = [
            step_id
            for step_id in (
                str(step.get("id") or "")
                for step in sorted(
                    (step_by_id[item] for item in visited_steps if item in step_by_id),
                    key=lambda item: int(item.get("step_number") or 0),
                )
            )
            if step_id
        ]
        direct_unlock_step_ids = [
            step_id
            for step_id in ordered_step_ids
            if (
                part_id in [str(item or "") for item in (step_by_id.get(step_id, {}).get("unlocks_part_ids") or [])]
                and bool(step_by_id.get(step_id, {}).get("release_valid", True))
            )
        ]
        part_paths[part_id] = {
            "part_id": part_id,
            "direct_step_ids": list(direct_step_ids),
            "path_step_ids": ordered_step_ids,
            "direct_unlock_step_ids": direct_unlock_step_ids,
            "step_count": len(ordered_step_ids),
        }
    return part_paths


def _ranges_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return min(float(end_a), float(end_b)) - max(float(start_a), float(start_b)) > EPS


def _rectangles_touch_or_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ax0 = float(a.get("x") or 0.0)
    ay0 = float(a.get("y") or 0.0)
    ax1 = ax0 + float(a.get("width") or 0.0)
    ay1 = ay0 + float(a.get("height") or 0.0)
    bx0 = float(b.get("x") or 0.0)
    by0 = float(b.get("y") or 0.0)
    bx1 = bx0 + float(b.get("width") or 0.0)
    by1 = by0 + float(b.get("height") or 0.0)
    overlap_x = min(ax1, bx1) - max(ax0, bx0)
    overlap_y = min(ay1, by1) - max(ay0, by0)
    if overlap_x > EPS and overlap_y > EPS:
        return True
    if abs(ax1 - bx0) <= EPS or abs(bx1 - ax0) <= EPS:
        return _ranges_overlap(ay0, ay1, by0, by1)
    if abs(ay1 - by0) <= EPS or abs(by1 - ay0) <= EPS:
        return _ranges_overlap(ax0, ax1, bx0, bx1)
    return False


def _rectangles_intersection_area(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    ax0 = float(a.get("x") or 0.0)
    ay0 = float(a.get("y") or 0.0)
    ax1 = ax0 + float(a.get("width") or 0.0)
    ay1 = ay0 + float(a.get("height") or 0.0)
    bx0 = float(b.get("x") or 0.0)
    by0 = float(b.get("y") or 0.0)
    bx1 = bx0 + float(b.get("width") or 0.0)
    by1 = by0 + float(b.get("height") or 0.0)
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    return width * height


def _rectangles_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return _rectangles_intersection_area(a, b) > EPS


def _workpiece_matches_part(workpiece: Dict[str, float], part: Dict[str, float]) -> bool:
    return (
        abs(float(workpiece.get("x") or 0.0) - float(part.get("x") or 0.0)) <= EPS
        and abs(float(workpiece.get("y") or 0.0) - float(part.get("y") or 0.0)) <= EPS
        and abs(float(workpiece.get("width") or 0.0) - float(part.get("width") or 0.0)) <= EPS
        and abs(float(workpiece.get("height") or 0.0) - float(part.get("height") or 0.0)) <= EPS
    )


def _workpiece_key_payload(workpiece: Dict[str, float]) -> Tuple[float, float, float, float]:
    return (
        round(float(workpiece.get("x") or 0.0), 4),
        round(float(workpiece.get("y") or 0.0), 4),
        round(float(workpiece.get("width") or 0.0), 4),
        round(float(workpiece.get("height") or 0.0), 4),
    )


def _part_payload(rect: Rect) -> Dict[str, Any]:
    return {
        "part_id": str(rect.part_id),
        "part_name": str(rect.part_name),
        "x": float(rect.x),
        "y": float(rect.y),
        "width": float(rect.width),
        "height": float(rect.height),
        "rotated": bool(rect.rotated),
    }


def _offcut_overlap_value(
    workpiece: Dict[str, float],
    offcut_regions: Sequence[Dict[str, Any]],
    *,
    protected_only: bool,
) -> float:
    if float(workpiece.get("width") or 0.0) <= EPS or float(workpiece.get("height") or 0.0) <= EPS:
        return 0.0
    aspect = min(float(workpiece["width"]), float(workpiece["height"])) / max(
        max(float(workpiece["width"]), float(workpiece["height"])),
        1.0,
    )
    shape_bonus = 0.6 + 0.4 * max(0.0, min(1.0, aspect))
    total = 0.0
    for region in offcut_regions:
        if not isinstance(region, dict):
            continue
        if protected_only and int(region.get("preserve_rank") or 999) > 3:
            continue
        protection = max(1.0, float(region.get("protection_priority") or 1.0))
        for rect in (region.get("rects") or []):
            if not isinstance(rect, dict):
                continue
            overlap_area = _rectangles_intersection_area(workpiece, rect)
            if overlap_area <= EPS:
                continue
            total += overlap_area * protection * shape_bonus
    return round(float(total), 6)


def _generate_through_cut_candidates(
    *,
    items: Sequence[Dict[str, Any]],
    workpiece: Dict[str, float],
    kerf_mm: float,
) -> List[Dict[str, Any]]:
    x0 = float(workpiece.get("x") or 0.0)
    y0 = float(workpiece.get("y") or 0.0)
    x1 = x0 + float(workpiece.get("width") or 0.0)
    y1 = y0 + float(workpiece.get("height") or 0.0)
    kerf = max(0.0, float(kerf_mm or 0.0))
    candidates: List[Dict[str, Any]] = []

    def add_vertical(x: float) -> None:
        band = {"x": float(x), "y": y0, "width": kerf, "height": y1 - y0}
        # A kerf can consume an entire edge strip, leaving only one workpiece.
        # Keep the full blade band even when part of it lies outside the stock:
        # clipping it would move the rendered/tool centerline into the part.
        if kerf > EPS:
            if x + kerf <= x0 + EPS or x >= x1 - EPS:
                return
        elif x <= x0 + EPS or x >= x1 - EPS:
            return
        if any(_rectangles_intersection_area(band, item) > EPS for item in items):
            return
        left_items = [item for item in items if float(item["x"]) + float(item["width"]) <= float(x) + EPS]
        right_items = [item for item in items if float(item["x"]) >= float(x) + kerf - EPS]
        if len(left_items) + len(right_items) != len(items):
            return
        left_piece = {"x": x0, "y": y0, "width": max(0.0, float(x) - x0), "height": y1 - y0}
        right_x = max(x0, float(x) + kerf)
        right_piece = {"x": right_x, "y": y0, "width": max(0.0, x1 - right_x), "height": y1 - y0}
        children = [
            {"workpiece": piece, "items": subset}
            for piece, subset in ((left_piece, left_items), (right_piece, right_items))
            if piece["width"] > EPS
        ]
        if not children:
            return
        candidates.append(
            {
                "axis": "vertical",
                "cut_band": band,
                "children": children,
            }
        )

    def add_horizontal(y: float) -> None:
        band = {"x": x0, "y": float(y), "width": x1 - x0, "height": kerf}
        if kerf > EPS:
            if y + kerf <= y0 + EPS or y >= y1 - EPS:
                return
        elif y <= y0 + EPS or y >= y1 - EPS:
            return
        if any(_rectangles_intersection_area(band, item) > EPS for item in items):
            return
        bottom_items = [item for item in items if float(item["y"]) + float(item["height"]) <= float(y) + EPS]
        top_items = [item for item in items if float(item["y"]) >= float(y) + kerf - EPS]
        if len(bottom_items) + len(top_items) != len(items):
            return
        bottom_piece = {"x": x0, "y": y0, "width": x1 - x0, "height": max(0.0, float(y) - y0)}
        top_y = max(y0, float(y) + kerf)
        top_piece = {"x": x0, "y": top_y, "width": x1 - x0, "height": max(0.0, y1 - top_y)}
        children = [
            {"workpiece": piece, "items": subset}
            for piece, subset in ((bottom_piece, bottom_items), (top_piece, top_items))
            if piece["height"] > EPS
        ]
        if not children:
            return
        candidates.append(
            {
                "axis": "horizontal",
                "cut_band": band,
                "children": children,
            }
        )

    seen_vertical: set[float] = set()
    seen_horizontal: set[float] = set()
    for item in items:
        for value in (float(item["x"]) - kerf, float(item["x"]) + float(item["width"])):
            rounded = round(value, 6)
            if rounded in seen_vertical:
                continue
            seen_vertical.add(rounded)
            add_vertical(value)
        for value in (float(item["y"]) - kerf, float(item["y"]) + float(item["height"])):
            rounded = round(value, 6)
            if rounded in seen_horizontal:
                continue
            seen_horizontal.add(rounded)
            add_horizontal(value)
    return candidates


def _collect_recursive_parts(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = str(node.get("kind") or "")
    if kind == "part":
        return [dict(node.get("part") or {})]
    parts: List[Dict[str, Any]] = []
    for child in (node.get("children") or []):
        if isinstance(child, dict):
            parts.extend(_collect_recursive_parts(child))
    return parts


def _covers_interval(cover_start: float, cover_end: float, target_start: float, target_end: float) -> bool:
    return float(cover_start) <= float(target_start) + EPS and float(cover_end) >= float(target_end) - EPS


def _target_side_blocked(
    side: str,
    *,
    workpiece: Dict[str, Any],
    target: Dict[str, Any],
    cut_band: Dict[str, Any] | None,
) -> bool:
    wx0 = float(workpiece.get("x") or 0.0)
    wy0 = float(workpiece.get("y") or 0.0)
    wx1 = wx0 + float(workpiece.get("width") or 0.0)
    wy1 = wy0 + float(workpiece.get("height") or 0.0)
    tx0 = float(target.get("x") or 0.0)
    ty0 = float(target.get("y") or 0.0)
    tx1 = tx0 + float(target.get("width") or 0.0)
    ty1 = ty0 + float(target.get("height") or 0.0)

    if side == "left":
        if abs(tx0 - wx0) <= EPS:
            return True
        if not cut_band:
            return False
        bx0 = float(cut_band.get("x") or 0.0)
        bx1 = bx0 + float(cut_band.get("width") or 0.0)
        by0 = float(cut_band.get("y") or 0.0)
        by1 = by0 + float(cut_band.get("height") or 0.0)
        return abs(bx1 - tx0) <= EPS and _covers_interval(by0, by1, ty0, ty1)

    if side == "right":
        if abs(tx1 - wx1) <= EPS:
            return True
        if not cut_band:
            return False
        bx0 = float(cut_band.get("x") or 0.0)
        by0 = float(cut_band.get("y") or 0.0)
        by1 = by0 + float(cut_band.get("height") or 0.0)
        return abs(bx0 - tx1) <= EPS and _covers_interval(by0, by1, ty0, ty1)

    if side == "top":
        if abs(ty0 - wy0) <= EPS:
            return True
        if not cut_band:
            return False
        bx0 = float(cut_band.get("x") or 0.0)
        bx1 = bx0 + float(cut_band.get("width") or 0.0)
        by0 = float(cut_band.get("y") or 0.0)
        by1 = by0 + float(cut_band.get("height") or 0.0)
        return abs(by1 - ty0) <= EPS and _covers_interval(bx0, bx1, tx0, tx1)

    if side == "bottom":
        if abs(ty1 - wy1) <= EPS:
            return True
        if not cut_band:
            return False
        bx0 = float(cut_band.get("x") or 0.0)
        bx1 = bx0 + float(cut_band.get("width") or 0.0)
        by0 = float(cut_band.get("y") or 0.0)
        return abs(by0 - ty1) <= EPS and _covers_interval(bx0, bx1, tx0, tx1)

    return False


def _step_releases_targets(
    step: Dict[str, Any],
    placements_by_id: Dict[str, Dict[str, float]],
) -> Tuple[bool, List[str]]:
    workpiece = step.get("workpiece_before") if isinstance(step.get("workpiece_before"), dict) else None
    cut_band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    target_ids = [str(item or "") for item in (step.get("unlocks_part_ids") or []) if str(item or "")]
    if not workpiece or not cut_band or not target_ids:
        return True, []

    failures: List[str] = []
    for part_id in target_ids:
        target = placements_by_id.get(part_id)
        if not isinstance(target, dict):
            continue
        open_sides = [
            side
            for side in ("left", "right", "top", "bottom")
            if not _target_side_blocked(side, workpiece=workpiece, target=target, cut_band=cut_band)
        ]
        if open_sides:
            failures.append(f"{part_id}:{','.join(open_sides)}")
    return len(failures) == 0, failures


def _clone_cut_step(step: Dict[str, Any]) -> Dict[str, Any]:
    cloned = dict(step)
    if isinstance(step.get("start"), dict):
        cloned["start"] = dict(step["start"])
    if isinstance(step.get("end"), dict):
        cloned["end"] = dict(step["end"])
    if isinstance(step.get("cut_band"), dict):
        cloned["cut_band"] = dict(step["cut_band"])
    if isinstance(step.get("workpiece_before"), dict):
        cloned["workpiece_before"] = dict(step["workpiece_before"])
    if isinstance(step.get("workpieces_after"), list):
        cloned["workpieces_after"] = [dict(item) for item in step["workpieces_after"] if isinstance(item, dict)]
    for key in ("target_part_ids", "target_part_names", "depends_on_step_ids", "workpiece_after_ids", "unlocks_part_ids"):
        if isinstance(step.get(key), list):
            cloned[key] = list(step[key])
    return cloned


def _step_intruded_region_ids(step: Dict[str, Any], offcut_regions: Sequence[Dict[str, Any]]) -> List[str]:
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if not band:
        return []
    hits: List[Tuple[str, float, int]] = []
    protected_ids = {str(item) for item in (step.get("protected_region_ids") or [])}
    for region in offcut_regions:
        region_id = str(region.get("id") or "")
        if not region_id:
            continue
        rects = [rect for rect in (region.get("rects") or []) if isinstance(rect, dict)]
        overlap_area = sum(_rectangles_intersection_area(band, rect) for rect in rects)
        if overlap_area <= EPS:
            continue
        hits.append(
            (
                region_id,
                overlap_area,
                1 if region_id in protected_ids else 0,
            )
        )
    hits.sort(key=lambda item: (-item[2], -item[1], item[0]))
    return [item[0] for item in hits]


def _step_offcut_damage_score(step: Dict[str, Any], offcut_regions: Sequence[Dict[str, Any]]) -> float:
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if not band:
        return 0.0
    score = 0.0
    for region in offcut_regions:
        rects = [rect for rect in (region.get("rects") or []) if isinstance(rect, dict)]
        overlap_area = sum(_rectangles_intersection_area(band, rect) for rect in rects)
        if overlap_area <= EPS:
            continue
        region_area = max(float(region.get("area_total") or 0.0), 1.0)
        protection = max(1.0, float(region.get("protection_priority") or 1.0))
        score += (overlap_area / region_area) * protection
    return round(float(score), 4)


def _step_hits_non_target_part(step: Dict[str, Any], placements_by_id: Dict[str, Dict[str, float]]) -> bool:
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if not band:
        return False
    target_ids = {str(item or "") for item in (step.get("target_part_ids") or [])}
    for part_id, rect in placements_by_id.items():
        if part_id in target_ids or not isinstance(rect, dict):
            continue
        if _rectangles_overlap(band, rect):
            return True
    return False


def _initial_offcut_rect_state(offcut_regions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rect_state: List[Dict[str, Any]] = []
    for region in offcut_regions:
        region_id = str(region.get("id") or "")
        protection_priority = float(region.get("protection_priority") or 1.0)
        for rect in (region.get("rects") or []):
            if not isinstance(rect, dict):
                continue
            rect_state.append(
                {
                    "region_id": region_id,
                    "x": float(rect.get("x") or 0.0),
                    "y": float(rect.get("y") or 0.0),
                    "width": float(rect.get("width") or 0.0),
                    "height": float(rect.get("height") or 0.0),
                    "area": float(rect.get("area") or (float(rect.get("width") or 0.0) * float(rect.get("height") or 0.0))),
                    "protection_priority": protection_priority,
                }
            )
    return rect_state


def _subtract_band_from_rect(rect: Dict[str, Any], band: Dict[str, Any]) -> List[Dict[str, Any]]:
    intersection = _rectangles_intersection_area(rect, band)
    if intersection <= EPS:
        return [dict(rect)]

    rx0 = float(rect.get("x") or 0.0)
    ry0 = float(rect.get("y") or 0.0)
    rx1 = rx0 + float(rect.get("width") or 0.0)
    ry1 = ry0 + float(rect.get("height") or 0.0)
    bx0 = float(band.get("x") or 0.0)
    by0 = float(band.get("y") or 0.0)
    bx1 = bx0 + float(band.get("width") or 0.0)
    by1 = by0 + float(band.get("height") or 0.0)

    ix0 = max(rx0, bx0)
    iy0 = max(ry0, by0)
    ix1 = min(rx1, bx1)
    iy1 = min(ry1, by1)

    pieces: List[Dict[str, Any]] = []

    def add_piece(x: float, y: float, width: float, height: float) -> None:
        if width <= EPS or height <= EPS:
            return
        next_rect = dict(rect)
        next_rect["x"] = float(x)
        next_rect["y"] = float(y)
        next_rect["width"] = float(width)
        next_rect["height"] = float(height)
        next_rect["area"] = float(width * height)
        pieces.append(next_rect)

    add_piece(rx0, ry0, rx1 - rx0, iy0 - ry0)
    add_piece(rx0, iy1, rx1 - rx0, ry1 - iy1)
    add_piece(rx0, iy0, ix0 - rx0, iy1 - iy0)
    add_piece(ix1, iy0, rx1 - ix1, iy1 - iy0)
    return pieces


def _apply_band_to_offcut_state(
    rect_state: Sequence[Dict[str, Any]],
    band: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    if not band:
        return [dict(item) for item in rect_state if isinstance(item, dict)]
    next_state: List[Dict[str, Any]] = []
    for rect in rect_state:
        if not isinstance(rect, dict):
            continue
        next_state.extend(_subtract_band_from_rect(rect, band))
    return next_state


def _offcut_damage_score_from_state(
    step: Dict[str, Any],
    rect_state: Sequence[Dict[str, Any]],
) -> float:
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if not band:
        return 0.0
    score = 0.0
    for rect in rect_state:
        if not isinstance(rect, dict):
            continue
        overlap_area = _rectangles_intersection_area(band, rect)
        if overlap_area <= EPS:
            continue
        area = max(float(rect.get("area") or 0.0), 1.0)
        protection = max(1.0, float(rect.get("protection_priority") or 1.0))
        score += (overlap_area / area) * protection
    return round(float(score), 4)


def _offcut_state_region_summary(rect_state: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    summary: Dict[str, Dict[str, float]] = {}
    for rect in rect_state:
        if not isinstance(rect, dict):
            continue
        region_id = str(rect.get("region_id") or "")
        if not region_id:
            continue
        entry = summary.setdefault(
            region_id,
            {
                "area_total": 0.0,
                "largest_rect_area": 0.0,
                "rect_count": 0.0,
                "protection_priority": float(rect.get("protection_priority") or 1.0),
            },
        )
        area = float(rect.get("area") or 0.0)
        entry["area_total"] += area
        entry["largest_rect_area"] = max(entry["largest_rect_area"], area)
        entry["rect_count"] += 1.0
    return summary


def _offcut_state_preservation_penalty(
    before_state: Sequence[Dict[str, Any]],
    after_state: Sequence[Dict[str, Any]],
    protected_region_ids: Sequence[str],
) -> float:
    before_summary = _offcut_state_region_summary(before_state)
    after_summary = _offcut_state_region_summary(after_state)
    score = 0.0
    for region_id in protected_region_ids:
        region_key = str(region_id or "")
        if not region_key:
            continue
        before = before_summary.get(region_key)
        if not before:
            continue
        after = after_summary.get(region_key, {"area_total": 0.0, "largest_rect_area": 0.0, "rect_count": 0.0})
        protection = max(1.0, float(before.get("protection_priority") or 1.0))
        before_area = max(1.0, float(before.get("area_total") or 0.0))
        before_largest = max(1.0, float(before.get("largest_rect_area") or 0.0))
        area_loss = max(0.0, float(before.get("area_total") or 0.0) - float(after.get("area_total") or 0.0)) / before_area
        largest_loss = max(0.0, float(before.get("largest_rect_area") or 0.0) - float(after.get("largest_rect_area") or 0.0)) / before_largest
        fragment_increase = max(0.0, float(after.get("rect_count") or 0.0) - float(before.get("rect_count") or 0.0))
        score += protection * (area_loss * 1.0 + largest_loss * 3.0 + fragment_increase * 0.12)
    return round(float(score), 6)


def _intruded_region_ids_from_state(
    step: Dict[str, Any],
    rect_state: Sequence[Dict[str, Any]],
) -> List[str]:
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if not band:
        return []
    hits: Dict[str, float] = {}
    for rect in rect_state:
        if not isinstance(rect, dict):
            continue
        overlap_area = _rectangles_intersection_area(band, rect)
        if overlap_area <= EPS:
            continue
        region_id = str(rect.get("region_id") or "")
        if not region_id:
            continue
        hits[region_id] = hits.get(region_id, 0.0) + overlap_area
    return [item[0] for item in sorted(hits.items(), key=lambda pair: (-pair[1], pair[0]))]


def _single_target_part_id(step: Dict[str, Any]) -> str | None:
    target_ids = [str(item or "") for item in (step.get("target_part_ids") or []) if str(item or "")]
    if len(target_ids) == 1:
        return target_ids[0]
    unlock_ids = [str(item or "") for item in (step.get("unlocks_part_ids") or []) if str(item or "")]
    if len(unlock_ids) == 1:
        return unlock_ids[0]
    return None


def _group_steps_by_single_target(base_steps: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current_group: List[Dict[str, Any]] = []
    current_part_id: str | None = None
    for step in base_steps:
        if not isinstance(step, dict):
            continue
        part_id = _single_target_part_id(step)
        if not part_id:
            if current_group:
                groups.append(current_group)
                current_group = []
                current_part_id = None
            groups.append([step])
            continue
        if current_group and part_id == current_part_id:
            current_group.append(step)
            continue
        if current_group:
            groups.append(current_group)
        current_group = [step]
        current_part_id = part_id
    if current_group:
        groups.append(current_group)
    return groups


def _build_stop_cut_candidate(
    step: Dict[str, Any],
    target: Dict[str, Any],
    overshoot_mm: float,
) -> Dict[str, Any] | None:
    axis = str(step.get("axis") or "")
    entry_side = str(step.get("entry_side") or "")
    workpiece = step.get("workpiece_before") if isinstance(step.get("workpiece_before"), dict) else None
    band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
    if axis not in {"horizontal", "vertical"} or not workpiece or not band or overshoot_mm <= 0.0:
        return None

    candidate_step = _clone_cut_step(step)
    if axis == "horizontal":
        x0 = float(workpiece["x"])
        x1 = float(workpiece["x"]) + float(workpiece["width"])
        if entry_side == "right":
            start_x = x1
            end_x = max(x0, float(target["x"]) - overshoot_mm)
        else:
            start_x = x0
            end_x = min(x1, float(target["x"]) + float(target["width"]) + overshoot_mm)
        new_left = min(start_x, end_x)
        new_right = max(start_x, end_x)
        candidate_step["cut_band"] = {
            "x": new_left,
            "y": float(band["y"]),
            "width": max(0.0, new_right - new_left),
            "height": float(band["height"]),
        }
        candidate_step["start"] = {"x": start_x, "y": float(step.get("start", {}).get("y") or 0.0)}
        candidate_step["end"] = {"x": end_x, "y": float(step.get("end", {}).get("y") or 0.0)}
    else:
        y0 = float(workpiece["y"])
        y1 = float(workpiece["y"]) + float(workpiece["height"])
        if entry_side == "top":
            start_y = y0
            end_y = min(y1, float(target["y"]) + float(target["height"]) + overshoot_mm)
        else:
            start_y = y1
            end_y = max(y0, float(target["y"]) - overshoot_mm)
        new_top = min(start_y, end_y)
        new_bottom = max(start_y, end_y)
        candidate_step["cut_band"] = {
            "x": float(band["x"]),
            "y": new_top,
            "width": float(band["width"]),
            "height": max(0.0, new_bottom - new_top),
        }
        candidate_step["start"] = {"x": float(step.get("start", {}).get("x") or 0.0), "y": start_y}
        candidate_step["end"] = {"x": float(step.get("end", {}).get("x") or 0.0), "y": end_y}

    if max(float(candidate_step["cut_band"]["width"]), float(candidate_step["cut_band"]["height"])) <= EPS:
        return None
    candidate_step["cut_type"] = "stop_cut"
    candidate_step["termination_reason"] = "part_release_with_overshoot"
    candidate_step["overshoot_mm"] = overshoot_mm
    return candidate_step


def _group_releases_target(steps: Sequence[Dict[str, Any]], target: Dict[str, Any]) -> Tuple[bool, List[str]]:
    failures: List[str] = []
    for side in ("left", "right", "top", "bottom"):
        blocked = False
        for step in steps:
            workpiece = step.get("workpiece_before") if isinstance(step.get("workpiece_before"), dict) else None
            cut_band = step.get("cut_band") if isinstance(step.get("cut_band"), dict) else None
            if workpiece and _target_side_blocked(side, workpiece=workpiece, target=target, cut_band=cut_band):
                blocked = True
                break
        if not blocked:
            failures.append(side)
    return len(failures) == 0, failures


def _build_recursive_cut_plan_trees(
    *,
    placements: Sequence[Rect],
    sheet_length: float,
    sheet_width: float,
    kerf_mm: float,
    offcut_regions: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[str, Any] | None]:
    items = [_part_payload(rect) for rect in placements]
    root_workpiece = {"x": 0.0, "y": 0.0, "width": float(sheet_length), "height": float(sheet_width)}
    memo: Dict[Tuple[Tuple[float, float, float, float], Tuple[str, ...]], Dict[str, Dict[str, Any] | None]] = {}
    offcut_value_cache: Dict[Tuple[Tuple[float, float, float, float], bool], float] = {}
    candidate_cache: Dict[Tuple[Tuple[float, float, float, float], Tuple[str, ...]], List[Dict[str, Any]]] = {}

    def signature(mode_name: str, summary: Dict[str, Any]) -> Tuple[float, float, float, float, float, float]:
        if mode_name == "protect":
            return (
                -float(summary.get("direct_protected_release_value") or 0.0),
                -float(summary.get("released_protected_value") or 0.0),
                -float(summary.get("direct_offcut_release_value") or 0.0),
                float(summary.get("step_count") or 0.0),
                float(summary.get("depth") or 0.0),
                -float(summary.get("released_part_count") or 0.0),
            )
        return (
            0.0 if float(summary.get("direct_part_release_count") or 0.0) > 0.0 else 1.0,
            0.0 if float(summary.get("direct_offcut_release_value") or 0.0) > 0.0 else 1.0,
            float(summary.get("step_count") or 0.0),
            float(summary.get("depth") or 0.0),
            -float(summary.get("released_part_count") or 0.0),
            -float(summary.get("released_offcut_value") or 0.0),
        )

    def overlap_value(workpiece: Dict[str, float], *, protected_only: bool) -> float:
        cache_key = (_workpiece_key_payload(workpiece), bool(protected_only))
        cached = offcut_value_cache.get(cache_key)
        if cached is not None:
            return cached
        value = _offcut_overlap_value(workpiece, offcut_regions, protected_only=protected_only)
        offcut_value_cache[cache_key] = value
        return value

    def solve(items_subset: Sequence[Dict[str, Any]], workpiece: Dict[str, float]) -> Dict[str, Dict[str, Any] | None]:
        key = (
            _workpiece_key_payload(workpiece),
            tuple(sorted(str(item.get("part_id") or "") for item in items_subset)),
        )
        if key in memo:
            cached = memo[key]
            return dict(cached)

        if not items_subset:
            result = {
                "kind": "offcut",
                "workpiece": dict(workpiece),
                "summary": {
                    "step_count": 0,
                    "depth": 0,
                    "released_part_count": 0,
                    "direct_part_release_count": 0,
                    "released_offcut_value": overlap_value(workpiece, protected_only=False),
                    "direct_offcut_release_value": overlap_value(workpiece, protected_only=False),
                    "released_protected_value": overlap_value(workpiece, protected_only=True),
                    "direct_protected_release_value": overlap_value(workpiece, protected_only=True),
                },
            }
            memo[key] = {"default": result, "protect": dict(result)}
            return dict(memo[key])

        if len(items_subset) == 1 and _workpiece_matches_part(workpiece, items_subset[0]):
            result = {
                "kind": "part",
                "workpiece": dict(workpiece),
                "part": dict(items_subset[0]),
                "summary": {
                    "step_count": 0,
                    "depth": 0,
                    "released_part_count": 1,
                    "direct_part_release_count": 1,
                    "released_offcut_value": 0.0,
                    "direct_offcut_release_value": 0.0,
                    "released_protected_value": 0.0,
                    "direct_protected_release_value": 0.0,
                },
            }
            memo[key] = {"default": result, "protect": dict(result)}
            return dict(memo[key])

        candidates = candidate_cache.get(key)
        if candidates is None:
            candidates = _generate_through_cut_candidates(items=items_subset, workpiece=workpiece, kerf_mm=kerf_mm)
            candidate_cache[key] = candidates
        best_results: Dict[str, Dict[str, Any] | None] = {"default": None, "protect": None}
        best_sigs: Dict[str, Tuple[float, float, float, float, float, float] | None] = {"default": None, "protect": None}
        for candidate in candidates:
            child_pair_results: List[Dict[str, Dict[str, Any] | None]] = []
            valid = True
            for child in candidate["children"]:
                child_result_pair = solve(child["items"], child["workpiece"])
                if child_result_pair.get("default") is None or child_result_pair.get("protect") is None:
                    valid = False
                    break
                child_pair_results.append(child_result_pair)
            if not valid:
                continue
            for mode_name in ("default", "protect"):
                child_results = [pair[mode_name] for pair in child_pair_results if isinstance(pair.get(mode_name), dict)]
                summary = {
                    "step_count": 1 + sum(int(child_result["summary"]["step_count"]) for child_result in child_results),
                    "depth": 1 + max(int(child_result["summary"]["depth"]) for child_result in child_results),
                    "released_part_count": sum(int(child_result["summary"]["released_part_count"]) for child_result in child_results),
                    "direct_part_release_count": sum(
                        1 for child_result in child_results if str(child_result.get("kind") or "") == "part"
                    ),
                    "released_offcut_value": sum(float(child_result["summary"]["released_offcut_value"]) for child_result in child_results),
                    "direct_offcut_release_value": sum(float(child_result["summary"]["direct_offcut_release_value"]) for child_result in child_results),
                    "released_protected_value": sum(float(child_result["summary"]["released_protected_value"]) for child_result in child_results),
                    "direct_protected_release_value": sum(float(child_result["summary"]["direct_protected_release_value"]) for child_result in child_results),
                }
                candidate_result = {
                    "kind": "cut",
                    "workpiece": dict(workpiece),
                    "axis": str(candidate["axis"]),
                    "cut_band": dict(candidate["cut_band"]),
                    "children": child_results,
                    "summary": summary,
                }
                current_sig = signature(mode_name, summary)
                if best_sigs[mode_name] is None or current_sig < best_sigs[mode_name]:
                    best_sigs[mode_name] = current_sig
                    best_results[mode_name] = candidate_result

        memo[key] = best_results
        return dict(best_results)

    return solve(items, root_workpiece)


def _build_recursive_cut_plan_steps(
    *,
    placements: Sequence[Rect],
    sheet_length: float,
    sheet_width: float,
    kerf_mm: float,
    sheet_id: int,
    source_strategy: str,
    mode: str,
    offcut_regions: Sequence[Dict[str, Any]],
    tree: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    items = [_part_payload(rect) for rect in placements]
    protected_region_ids = [
        str(item.get("id") or "")
        for item in offcut_regions
        if int(item.get("preserve_rank") or 999) <= 3
    ]
    if tree is None:
        trees = _build_recursive_cut_plan_trees(
            placements=placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            kerf_mm=kerf_mm,
            offcut_regions=offcut_regions,
        )
        tree = trees.get(mode)
    if not isinstance(tree, dict):
        return []

    recorder = CutPlanRecorder(kerf_mm=kerf_mm, sheet_id=sheet_id, source_strategy=f"{source_strategy}-{mode}")

    def emit(node: Dict[str, Any]) -> None:
        if str(node.get("kind") or "") != "cut":
            return
        children = [child for child in (node.get("children") or []) if isinstance(child, dict)]
        workpiece_before = dict(node.get("workpiece") or {})
        workpieces_after = [dict(child.get("workpiece") or {}) for child in children]
        target_items = _collect_recursive_parts(node)
        unlocks = [
            str(child.get("part", {}).get("part_id") or "")
            for child in children
            if str(child.get("kind") or "") == "part" and str(child.get("part", {}).get("part_id") or "")
        ]
        recorder.record_custom_cut(
            axis=str(node.get("axis") or ""),
            cut_band=dict(node.get("cut_band") or {}),
            workpiece_before=workpiece_before,
            workpieces_after=workpieces_after,
            target_part_ids=[str(item.get("part_id") or "") for item in target_items if str(item.get("part_id") or "")],
            target_part_names=[str(item.get("part_name") or "Part") for item in target_items if str(item.get("part_name") or "")],
            anchor="bl",
            unlocks_part_ids=unlocks,
        )
        for child in children:
            emit(child)

    emit(tree)
    placements_by_id = {
        str(item.get("part_id") or ""): item
        for item in items
        if str(item.get("part_id") or "")
    }
    steps: List[Dict[str, Any]] = []
    for raw_step in recorder.steps:
        step = _clone_cut_step(raw_step)
        step["cut_mode"] = mode
        step["cut_type"] = "through_cut"
        step["termination_reason"] = "board_edge"
        step["overshoot_mm"] = 0.0
        step["protected_region_ids"] = list(protected_region_ids)
        step["intruded_offcut_region_ids"] = _step_intruded_region_ids(step, offcut_regions)
        step["offcut_damage_score"] = _step_offcut_damage_score(step, offcut_regions)
        release_valid, release_failures = _step_releases_targets(step, placements_by_id)
        step["release_valid"] = bool(release_valid)
        step["release_failures"] = list(release_failures)
        steps.append(step)
    return steps


def _build_cut_plan_modes(
    *,
    base_steps: Sequence[Dict[str, Any]],
    placements: Sequence[Rect],
    offcut_regions: Sequence[Dict[str, Any]],
    kerf_mm: float,
    sheet_length: float | None = None,
    sheet_width: float | None = None,
    sheet_id: int | None = None,
    source_strategy: str = "layout",
) -> Dict[str, Dict[str, Any]]:
    overshoot_mm = round(min(max(0.0, float(kerf_mm or 0.0)) * 5.0, 30.0), 4)
    protected_region_ids = [str(item.get("id") or "") for item in offcut_regions if int(item.get("preserve_rank") or 0) <= 3]

    if sheet_length is not None and sheet_width is not None and sheet_id is not None:
        recursive_trees = _build_recursive_cut_plan_trees(
            placements=placements,
            sheet_length=float(sheet_length),
            sheet_width=float(sheet_width),
            kerf_mm=kerf_mm,
            offcut_regions=offcut_regions,
        )
        default_recursive = _build_recursive_cut_plan_steps(
            placements=placements,
            sheet_length=float(sheet_length),
            sheet_width=float(sheet_width),
            kerf_mm=kerf_mm,
            sheet_id=int(sheet_id),
            source_strategy=source_strategy,
            mode="default",
            offcut_regions=offcut_regions,
            tree=recursive_trees.get("default"),
        ) if isinstance(recursive_trees.get("default"), dict) else []
        protect_recursive = _build_recursive_cut_plan_steps(
            placements=placements,
            sheet_length=float(sheet_length),
            sheet_width=float(sheet_width),
            kerf_mm=kerf_mm,
            sheet_id=int(sheet_id),
            source_strategy=source_strategy,
            mode="protect",
            offcut_regions=offcut_regions,
            tree=recursive_trees.get("protect"),
        ) if isinstance(recursive_trees.get("protect"), dict) else []
        if default_recursive:
            return {
                "default": {
                    "mode": "default",
                    "steps": default_recursive,
                    "part_cut_paths": _build_part_cut_paths(default_recursive),
                    "meta": {
                        "mode": "default",
                        "step_count": len(default_recursive),
                        "overshoot_mm": 0.0,
                        "protected_region_ids": list(protected_region_ids),
                        "solver": "recursive_through_cut_v1",
                    },
                },
                "protect": {
                    "mode": "protect",
                    "steps": protect_recursive or [dict(step) for step in default_recursive],
                    "part_cut_paths": _build_part_cut_paths(protect_recursive or default_recursive),
                    "meta": {
                        "mode": "protect",
                        "step_count": len(protect_recursive or default_recursive),
                        "overshoot_mm": overshoot_mm,
                        "protected_region_ids": list(protected_region_ids),
                        "solver": "recursive_through_cut_v1",
                    },
                },
            }

    if not base_steps:
        return {
            "default": {
                "mode": "default",
                "steps": [],
                "part_cut_paths": {},
                "meta": {"mode": "default", "step_count": 0, "overshoot_mm": 0.0},
            },
            "protect": {
                "mode": "protect",
                "steps": [],
                "part_cut_paths": {},
                "meta": {"mode": "protect", "step_count": 0, "overshoot_mm": overshoot_mm},
            },
        }

    placements_by_id = {
        str(getattr(rect, "part_id", "") or ""): _rect_payload(rect)
        for rect in placements
        if str(getattr(rect, "part_id", "") or "")
    }

    default_steps: List[Dict[str, Any]] = []
    for raw_step in base_steps:
        if not isinstance(raw_step, dict):
            continue
        default_step = _clone_cut_step(raw_step)
        default_step["cut_mode"] = "default"
        default_step["cut_type"] = "through_cut"
        default_step["termination_reason"] = "board_edge"
        default_step["overshoot_mm"] = 0.0
        default_step["protected_region_ids"] = list(protected_region_ids)
        default_step["intruded_offcut_region_ids"] = _step_intruded_region_ids(default_step, offcut_regions)
        default_step["offcut_damage_score"] = _step_offcut_damage_score(default_step, offcut_regions)
        default_release_valid, default_release_failures = _step_releases_targets(default_step, placements_by_id)
        default_step["release_valid"] = bool(default_release_valid)
        default_step["release_failures"] = list(default_release_failures)
        default_steps.append(default_step)

    protect_steps: List[Dict[str, Any]] = []
    current_offcut_state = _initial_offcut_rect_state(offcut_regions)
    step_groups = _group_steps_by_single_target(base_steps)

    for group in step_groups:
        if not group:
            continue
        part_id = _single_target_part_id(group[0]) if len(group) > 0 else None
        target = placements_by_id.get(str(part_id or "")) if part_id else None

        if not part_id or not isinstance(target, dict):
            for raw_step in group:
                protect_step = _clone_cut_step(raw_step)
                protect_step["cut_mode"] = "protect"
                protect_step["cut_type"] = "through_cut"
                protect_step["termination_reason"] = "board_edge"
                protect_step["overshoot_mm"] = 0.0
                protect_step["protected_region_ids"] = list(protected_region_ids)
                protect_step["intruded_offcut_region_ids"] = _intruded_region_ids_from_state(protect_step, current_offcut_state)
                protect_step["offcut_damage_score"] = _offcut_damage_score_from_state(protect_step, current_offcut_state)
                protect_release_valid, protect_release_failures = _step_releases_targets(protect_step, placements_by_id)
                protect_step["release_valid"] = bool(protect_release_valid)
                protect_step["release_failures"] = list(protect_release_failures)
                protect_steps.append(protect_step)
                current_offcut_state = _apply_band_to_offcut_state(
                    current_offcut_state,
                    protect_step.get("cut_band") if isinstance(protect_step.get("cut_band"), dict) else None,
                )
            continue

        option_sets: List[List[Dict[str, Any]]] = [[]]
        for raw_step in group:
            base_option = _clone_cut_step(raw_step)
            base_option["cut_mode"] = "protect"
            base_option["cut_type"] = "through_cut"
            base_option["termination_reason"] = "board_edge"
            base_option["overshoot_mm"] = 0.0
            base_option["protected_region_ids"] = list(protected_region_ids)

            choices = [base_option]
            candidate_stop = _build_stop_cut_candidate(base_option, target, overshoot_mm)
            if (
                candidate_stop
                and not _step_hits_non_target_part(candidate_stop, placements_by_id)
            ):
                candidate_stop["cut_mode"] = "protect"
                candidate_stop["protected_region_ids"] = list(protected_region_ids)
                choices.append(candidate_stop)

            next_option_sets: List[List[Dict[str, Any]]] = []
            for existing in option_sets:
                for choice in choices:
                    next_option_sets.append(existing + [_clone_cut_step(choice)])
            option_sets = next_option_sets

        best_group_steps: List[Dict[str, Any]] | None = None
        best_group_signature: Tuple[float, float, float, float] | None = None
        for option_group in option_sets:
            release_valid, release_failures = _group_releases_target(option_group, target)
            if not release_valid:
                continue
            temp_state = [dict(item) for item in current_offcut_state]
            total_damage = 0.0
            stop_count = 0
            intruded_protected_count = 0
            valid_group = True
            for candidate_step in option_group:
                if _step_hits_non_target_part(candidate_step, placements_by_id):
                    valid_group = False
                    break
                damage = _offcut_damage_score_from_state(candidate_step, temp_state)
                intruded_region_ids = _intruded_region_ids_from_state(candidate_step, temp_state)
                candidate_step["intruded_offcut_region_ids"] = intruded_region_ids
                candidate_step["offcut_damage_score"] = damage
                candidate_step["release_valid"] = True
                candidate_step["release_failures"] = list(release_failures if candidate_step is option_group[-1] else [])
                total_damage += damage
                intruded_protected_count += sum(
                    1 for region_id in intruded_region_ids if str(region_id or "") in protected_region_ids
                )
                if str(candidate_step.get("cut_type") or "") == "stop_cut":
                    stop_count += 1
                temp_state = _apply_band_to_offcut_state(
                    temp_state,
                    candidate_step.get("cut_band") if isinstance(candidate_step.get("cut_band"), dict) else None,
                )
            if not valid_group:
                continue
            preservation_penalty = _offcut_state_preservation_penalty(
                current_offcut_state,
                temp_state,
                protected_region_ids,
            )
            signature = (
                preservation_penalty,
                round(float(total_damage), 6),
                float(intruded_protected_count),
                float(stop_count),
            )
            if best_group_signature is None or signature < best_group_signature:
                best_group_signature = signature
                best_group_steps = [_clone_cut_step(item) for item in option_group]

        chosen_group = best_group_steps
        if not chosen_group:
            chosen_group = []
            for raw_step in group:
                protect_step = _clone_cut_step(raw_step)
                protect_step["cut_mode"] = "protect"
                protect_step["cut_type"] = "through_cut"
                protect_step["termination_reason"] = "board_edge"
                protect_step["overshoot_mm"] = 0.0
                protect_step["protected_region_ids"] = list(protected_region_ids)
                protect_step["intruded_offcut_region_ids"] = _intruded_region_ids_from_state(protect_step, current_offcut_state)
                protect_step["offcut_damage_score"] = _offcut_damage_score_from_state(protect_step, current_offcut_state)
                protect_release_valid, protect_release_failures = _step_releases_targets(protect_step, placements_by_id)
                protect_step["release_valid"] = bool(protect_release_valid)
                protect_step["release_failures"] = list(protect_release_failures)
                chosen_group.append(protect_step)

        for index, protect_step in enumerate(chosen_group):
            if "intruded_offcut_region_ids" not in protect_step:
                protect_step["intruded_offcut_region_ids"] = _intruded_region_ids_from_state(protect_step, current_offcut_state)
            if "offcut_damage_score" not in protect_step:
                protect_step["offcut_damage_score"] = _offcut_damage_score_from_state(protect_step, current_offcut_state)
            if "release_valid" not in protect_step:
                release_valid, release_failures = _group_releases_target(chosen_group[: index + 1], target)
                protect_step["release_valid"] = bool(release_valid if index == len(chosen_group) - 1 else True)
                protect_step["release_failures"] = list(release_failures if index == len(chosen_group) - 1 else [])
            protect_steps.append(protect_step)
            current_offcut_state = _apply_band_to_offcut_state(
                current_offcut_state,
                protect_step.get("cut_band") if isinstance(protect_step.get("cut_band"), dict) else None,
            )

    return {
        "default": {
            "mode": "default",
            "steps": default_steps,
            "part_cut_paths": _build_part_cut_paths(default_steps),
            "meta": {
                "mode": "default",
                "step_count": len(default_steps),
                "overshoot_mm": 0.0,
                "protected_region_ids": list(protected_region_ids),
            },
        },
        "protect": {
            "mode": "protect",
            "steps": protect_steps,
            "part_cut_paths": _build_part_cut_paths(protect_steps),
            "meta": {
                "mode": "protect",
                "step_count": len(protect_steps),
                "overshoot_mm": overshoot_mm,
                "protected_region_ids": list(protected_region_ids),
            },
        },
    }

class AdaptiveFrontierBuildV1Algorithm:
    FAST_SLICING_MAX_DEPTH = 2
    FAST_SLICING_MAX_AXIS_CUTS = 2
    FAST_SLICING_FALLBACK_DEPTH = 3
    FAST_SLICING_FALLBACK_AXIS_CUTS = 4
    STRUCTURED_PRE_DEDUPE_WARMUP = 80
    STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_MIN_PLACEMENTS = 8
    STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_WARMUP = 12
    STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_DUPLICATES = 6
    STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_RATE = 0.20
    STRUCTURED_PRE_DEDUPE_FAST_START_WARMUP = 24
    STRUCTURED_PRE_DEDUPE_FAST_START_DUPLICATES = 12
    STRUCTURED_PRE_DEDUPE_FAST_START_RATE = 0.20
    STRUCTURED_PRE_DEDUPE_MIN_RATE = 0.25
    STRUCTURED_REPEATED_PIECE_RATIO_MIN = 0.18
    STRUCTURED_SIZE_CLUSTER_STRENGTH_MIN = 0.45
    RANDOM_PRIMARY_DIMENSION_BUCKET_COUNT_MIN = 10
    RANDOM_ASPECT_CLUSTER_STRENGTH_MAX = 0.30
    RANDOM_REPEATED_PIECE_RATIO_MAX = 0.12

    def __init__(
        self,
        kerf_mm: float = 3.0,
        allow_rotation: bool = True,
        seed_limit: int = 5,
        candidate_pool_limit: int = 18,
        random_seed: int = 20261011,
        partition_max_tries: int = 6000,
        seed_early_stop: bool = True,
        deadline: float | None = None,
    ) -> None:
        self.kerf = float(kerf_mm)
        self.allow_rotation = allow_rotation
        self.seed_limit = max(2, int(seed_limit))
        self.candidate_pool_limit = max(8, int(candidate_pool_limit))
        self.random_seed = random_seed
        self.partition_max_tries = partition_max_tries
        self.seed_early_stop = seed_early_stop
        self.deadline = deadline
        self._verbose_stage_logs_enabled = False
        self._profile_stats: Dict[str, float] = {}

    def _normalize_candidates(
        self,
        raw: Any,
        *,
        width: float = 0.0,
        height: float = 0.0,
    ) -> List[Dict[str, Any]]:
        if raw:
            seen: set[tuple[float, float]] = set()
            out: list[dict[str, Any]] = []
            for item in raw:
                cand_width = float(item.get("width") or 0.0)
                cand_height = float(item.get("height") or 0.0)
                if cand_width <= 0.0 or cand_height <= 0.0:
                    continue
                key = (round(cand_width, 4), round(cand_height, 4))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "width": cand_width,
                        "height": cand_height,
                        "rotated": bool(item.get("rotated", False)),
                    }
                )
            if out:
                return out

        normalized_width = float(width or 0.0)
        normalized_height = float(height or 0.0)
        out = [{"width": normalized_width, "height": normalized_height, "rotated": False}]
        if self.allow_rotation and abs(normalized_width - normalized_height) > EPS:
            out.append({"width": normalized_height, "height": normalized_width, "rotated": True})
        return out

    def _free_rects_signature(self, free_rects: Sequence[FreeRect]) -> tuple[tuple[float, float, float, float], ...]:
        return tuple(
            (round(rect.x, 4), round(rect.y, 4), round(rect.width, 4), round(rect.height, 4))
            for rect in free_rects
        )

    def _profile_reset(self) -> None:
        self._profile_stats = {}

    def _profile_add(self, name: str, delta_ms: float, *, count: float = 1.0) -> None:
        self._profile_stats[f"{name}_ms"] = self._profile_stats.get(f"{name}_ms", 0.0) + max(0.0, float(delta_ms))
        self._profile_stats[f"{name}_count"] = self._profile_stats.get(f"{name}_count", 0.0) + float(count)

    def _profile_log(self, event: str, **fields: Any) -> None:
        if not self._verbose_stage_logs_enabled:
            return
        parts = [f"evt=adaptive_release event={event}"]
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, float):
                parts.append(f"{key}={value:.1f}")
            else:
                parts.append(f"{key}={value}")
        append_optimize_trace(" ".join(parts))

    def _profile_summary_fields(self) -> Dict[str, float]:
        keys = (
            "choose_next",
            "evaluate_candidate",
            "candidate_pool",
            "future_watchlist",
            "future_watchlist_after_host",
            "candidate_positions",
            "can_place",
            "update_free_rects",
            "fit_host_stats",
            "fast_slicing_proxy",
            "free_rect_metrics",
            "candidate_free_rect_metrics",
            "attachment_score",
            "void_cell_metrics",
            "component_count",
            "guillotine_metrics",
            "junction_metrics",
            "optimize_sheet",
            "seed_layout",
            "cleanup_sheet",
            "cleanup_layout",
            "cleanup_strip",
            "cleanup_partition",
            "cleanup_guillotine_area",
            "cleanup_guillotine_max_side",
            "cleanup_guillotine_short_side",
            "cleanup_guillotine_perimeter",
            "candidate_signature_observed",
            "candidate_signature_exact_duplicate",
            "candidate_signature_unique",
            "candidate_signature_duplicate_best_candidate",
            "candidate_signature_duplicate_nonwinner",
            "pre_signature_observed",
            "pre_signature_exact_duplicate",
            "pre_signature_unique",
            "pre_signature_duplicate_best_candidate",
            "pre_signature_duplicate_nonwinner",
            "pre_signature_candidate_exact_overlap",
            "pre_signature_candidate_mismatch",
            "pre_signature_build",
            "pre_key_l1_build",
            "pre_key_l1_observed",
            "pre_key_l1_collision",
            "pre_key_l2_build",
            "pre_key_l2_exact_match",
            "candidate_dedupe_hit",
            "pre_signature_reuse_eligible",
            "pre_signature_reuse_blocked_by_warmup",
            "pre_signature_reuse_blocked_by_duplicate_best",
            "pre_signature_reuse_blocked_by_mode",
            "pre_signature_reuse_executed",
            "reused_eval_ms_lt_0_1",
            "reused_eval_ms_0_1_to_0_25",
            "reused_eval_ms_0_25_to_0_5",
            "reused_eval_ms_gte_0_5",
            "executed_eval_ms_lt_0_1",
            "executed_eval_ms_0_1_to_0_25",
            "executed_eval_ms_0_25_to_0_5",
            "executed_eval_ms_gte_0_5",
        )
        if not self._verbose_stage_logs_enabled:
            keys = tuple(
                key
                for key in keys
                if key
                not in {
                    "candidate_signature_observed",
                    "candidate_signature_exact_duplicate",
                    "candidate_signature_unique",
                    "candidate_signature_duplicate_best_candidate",
                    "candidate_signature_duplicate_nonwinner",
                    "pre_signature_candidate_exact_overlap",
                    "pre_signature_candidate_mismatch",
                    "pre_signature_build",
                    "pre_key_l1_build",
                    "pre_key_l1_observed",
                    "pre_key_l1_collision",
                    "pre_key_l2_build",
                    "pre_key_l2_exact_match",
                    "reused_eval_ms_lt_0_1",
                    "reused_eval_ms_0_1_to_0_25",
                    "reused_eval_ms_0_25_to_0_5",
                    "reused_eval_ms_gte_0_5",
                    "executed_eval_ms_lt_0_1",
                    "executed_eval_ms_0_1_to_0_25",
                    "executed_eval_ms_0_25_to_0_5",
                    "executed_eval_ms_gte_0_5",
                }
            )
        summary: Dict[str, float] = {}
        for key in keys:
            ms = self._profile_stats.get(f"{key}_ms")
            count = self._profile_stats.get(f"{key}_count")
            if ms:
                summary[f"{key}_ms"] = round(ms, 1)
            if count:
                summary[f"{key}_count"] = int(count)
        candidate_pool_size_total = self._profile_stats.get("candidate_pool_size_total")
        candidate_pool_size_count = self._profile_stats.get("candidate_pool_size_count")
        if candidate_pool_size_total and candidate_pool_size_count:
            summary["candidate_pool_avg"] = round(candidate_pool_size_total / max(candidate_pool_size_count, 1.0), 1)
        future_watchlist_size_total = self._profile_stats.get("future_watchlist_size_total")
        future_watchlist_size_count = self._profile_stats.get("future_watchlist_size_count")
        if future_watchlist_size_total and future_watchlist_size_count:
            summary["future_watchlist_avg"] = round(
                future_watchlist_size_total / max(future_watchlist_size_count, 1.0),
                1,
            )
        evaluate_ms = summary.get("evaluate_candidate_ms")
        slicing_ms = summary.get("fast_slicing_proxy_ms")
        if evaluate_ms is not None and slicing_ms is not None:
            non_slicing_ms = max(0.0, evaluate_ms - slicing_ms)
            summary["evaluate_candidate_non_slicing_ms"] = round(non_slicing_ms, 1)
            known_non_slicing = (
                float(summary.get("update_free_rects_ms") or 0.0)
                + float(summary.get("candidate_free_rect_metrics_ms") or 0.0)
                + float(summary.get("attachment_score_ms") or 0.0)
                + float(summary.get("future_watchlist_after_host_ms") or 0.0)
            )
            summary["evaluate_candidate_other_ms"] = round(max(0.0, non_slicing_ms - known_non_slicing), 1)
        if self._verbose_stage_logs_enabled:
            observed = float(summary.get("candidate_signature_observed_count") or 0.0)
            exact_duplicates = float(summary.get("candidate_signature_exact_duplicate_count") or 0.0)
            unique = float(summary.get("candidate_signature_unique_count") or 0.0)
            if observed > 0.0:
                summary["candidate_duplicate_rate"] = round(exact_duplicates / observed, 4)
            if exact_duplicates <= 0.0 and observed > 0.0 and unique <= 0.0:
                summary["candidate_signature_unique_count"] = int(max(observed - exact_duplicates, 0.0))
        pre_observed = float(summary.get("pre_signature_observed_count") or 0.0)
        pre_exact_duplicates = float(summary.get("pre_signature_exact_duplicate_count") or 0.0)
        pre_unique = float(summary.get("pre_signature_unique_count") or 0.0)
        if pre_observed > 0.0:
            summary["pre_signature_duplicate_rate"] = round(pre_exact_duplicates / pre_observed, 4)
        if pre_exact_duplicates <= 0.0 and pre_observed > 0.0 and pre_unique <= 0.0:
            summary["pre_signature_unique_count"] = int(max(pre_observed - pre_exact_duplicates, 0.0))
        candidate_dedupe_saved_evals = int(round(float(self._profile_stats.get("candidate_dedupe_saved_evals") or 0.0)))
        candidate_dedupe_saved_ms_est = float(self._profile_stats.get("candidate_dedupe_saved_ms_est") or 0.0)
        candidate_dedupe_active_choose_next = int(
            round(float(self._profile_stats.get("candidate_dedupe_active_choose_next_count") or 0.0))
        )
        summary["candidate_dedupe_mode"] = "general_exact_dedupe" if candidate_dedupe_active_choose_next > 0 else "observe_only"
        summary["candidate_dedupe_hits"] = int(round(float(summary.get("candidate_dedupe_hit_count") or 0.0)))
        summary["candidate_dedupe_saved_evals"] = candidate_dedupe_saved_evals
        summary["candidate_dedupe_saved_ms_est"] = round(candidate_dedupe_saved_ms_est, 1)
        summary["candidate_dedupe_active_choose_next_count"] = candidate_dedupe_active_choose_next
        if self._verbose_stage_logs_enabled:
            pre_theoretical_savings_ms = 0.0
            if pre_observed > 0.0:
                pre_avg_eval_ms = float(summary.get("evaluate_candidate_ms") or 0.0) / max(pre_observed, 1.0)
                pre_theoretical_savings_ms = pre_avg_eval_ms * pre_exact_duplicates
            summary["pre_theoretical_savings_ms"] = round(pre_theoretical_savings_ms, 1)
            actual_saved_ms = candidate_dedupe_saved_ms_est
            summary["actual_saved_ms_est"] = round(actual_saved_ms, 1)
            summary["capture_ratio"] = round(actual_saved_ms / pre_theoretical_savings_ms, 4) if pre_theoretical_savings_ms > 0 else 0.0
            summary["net_est_ms"] = round(actual_saved_ms - float(summary.get("pre_signature_build_ms") or 0.0), 1)
        return summary

    def _record_eval_cost_bucket(self, prefix: str, eval_ms: float) -> None:
        if not self._verbose_stage_logs_enabled:
            return
        if eval_ms < 0.1:
            bucket = "lt_0_1"
        elif eval_ms < 0.25:
            bucket = "0_1_to_0_25"
        elif eval_ms < 0.5:
            bucket = "0_25_to_0_5"
        else:
            bucket = "gte_0_5"
        self._profile_add(f"{prefix}_{bucket}", 0.0)

    def _timed_fit_host_stats(
        self,
        part: Dict[str, Any],
        free_rects: Sequence[FreeRect],
        *,
        state_view: ChooseNextStateView | None = None,
        cache: dict[tuple[str, tuple[tuple[float, float, float, float], ...]], Dict[str, float]] | None = None,
    ) -> Dict[str, float]:
        if state_view is not None:
            return state_view.get_host_stats(part)
        cache_key: tuple[str, tuple[tuple[float, float, float, float], ...]] | None = None
        if cache is not None:
            cache_key = (str(part.get("id") or ""), self._free_rects_signature(free_rects))
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        started = time.perf_counter()
        result = self._fit_host_stats(part, free_rects)
        self._profile_add("fit_host_stats", (time.perf_counter() - started) * 1000.0)
        if cache is not None and cache_key is not None:
            cache[cache_key] = result
        return result

    def _timed_free_rect_metrics(
        self,
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("free_rect_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_candidate_free_rect_metrics(
        self,
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._candidate_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._profile_add("free_rect_metrics", elapsed_ms)
        self._profile_add("candidate_free_rect_metrics", elapsed_ms)
        return result

    def _timed_progress_free_rect_metrics(
        self,
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._progress_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("free_rect_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_can_place(
        self,
        rect: Rect,
        placements: Sequence[Rect],
        sheet_length: float,
        sheet_width: float,
        *,
        overlap_candidates: Sequence[Rect] | None = None,
    ) -> bool:
        started = time.perf_counter()
        result = self._can_place(rect, placements, sheet_length, sheet_width, overlap_candidates=overlap_candidates)
        self._profile_add("can_place", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_update_free_rects(
        self,
        free_rects: Sequence[FreeRect],
        *,
        host_index: int,
        rect: Rect,
        anchor: str,
    ) -> List[FreeRect]:
        started = time.perf_counter()
        result = self._update_free_rects(free_rects, host_index=host_index, rect=rect, anchor=anchor)
        self._profile_add("update_free_rects", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_candidate_positions(self, free_rect: FreeRect, width: float, height: float) -> List[Tuple[str, float, float]]:
        started = time.perf_counter()
        result = self._candidate_positions(free_rect, width, height)
        self._profile_add("candidate_positions", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_enclosed_void_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("void_cell_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_fast_slicing_proxy(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._fast_slicing_proxy(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("fast_slicing_proxy", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_void_cell_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._void_cell_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("void_cell_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_component_count(self, placements: Sequence[Rect]) -> int:
        started = time.perf_counter()
        result = self._component_count(placements)
        self._profile_add("component_count", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_guillotine_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, int | bool]:
        started = time.perf_counter()
        result = self._guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("guillotine_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _timed_junction_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        started = time.perf_counter()
        result = self._junction_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        self._profile_add("junction_metrics", (time.perf_counter() - started) * 1000.0)
        return result

    def _get_candidates(self, part: Dict[str, Any]) -> List[Dict[str, Any]]:
        cached = part.get("_normalized_candidates")
        if isinstance(cached, list) and cached:
            return cached
        out = self._normalize_candidates(
            part.get("candidates"),
            width=float(part.get("width", 0.0) or 0.0),
            height=float(part.get("height", 0.0) or 0.0),
        )
        part["_normalized_candidates"] = out
        return out

    def _part_stats(self, part: Dict[str, Any]) -> Dict[str, float]:
        candidates = self._get_candidates(part)
        max_area = max((float(item["width"]) * float(item["height"]) for item in candidates), default=0.0)
        max_side = max((max(float(item["width"]), float(item["height"])) for item in candidates), default=0.0)
        min_side = max((min(float(item["width"]), float(item["height"])) for item in candidates), default=0.0)
        aspect = max_side / max(min_side, 1.0)
        return {
            "max_area": max_area,
            "max_side": max_side,
            "min_side": min_side,
            "aspect": aspect,
        }

    def _expand_parts(self, parts: Sequence[Any], sheet_length: float, sheet_width: float) -> List[Dict[str, Any]]:
        sheet_area = max(sheet_length * sheet_width, 1.0)
        expanded: List[Dict[str, Any]] = []
        for index, part in enumerate(parts):
            qty = int(getattr(part, "quantity", 1) or 1)
            candidates = self._normalize_candidates(
                getattr(part, "candidates", None),
                width=float(getattr(part, "length_mm", 0.0) or 0.0),
                height=float(getattr(part, "width_mm", 0.0) or 0.0),
            )
            base = {
                "name": str(getattr(part, "name", "Part") or "Part"),
                "candidates": candidates,
                "_normalized_candidates": candidates,
            }
            stats = self._part_stats(base)
            hardness = (
                (stats["max_area"] / sheet_area) * 5.0
                + (stats["max_side"] / max(1.0, min(sheet_length, sheet_width))) * 4.0
                + min(6.0, stats["aspect"]) * 0.65
            )
            for copy_index in range(qty):
                part_id = str(getattr(part, "id", f"part-{index}"))
                if qty > 1:
                    part_id = f"{part_id}:{copy_index}"
                expanded.append(
                    {
                        "id": part_id,
                        "name": base["name"],
                        "candidates": candidates,
                        "_normalized_candidates": candidates,
                        "max_area": stats["max_area"],
                        "max_side": stats["max_side"],
                        "min_side": stats["min_side"],
                        "aspect": stats["aspect"],
                        "hardness": hardness,
                    }
                )
        expanded.sort(
            key=lambda item: (
                float(item["hardness"]),
                float(item["max_area"]),
                float(item["max_side"]),
            ),
            reverse=True,
        )
        return expanded

    def _board_structure_profile(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, Any]:
        part_count = len(parts)
        if part_count <= 0:
            return {
                "part_count": 0,
                "repeated_piece_ratio": 0.0,
                "size_cluster_strength": 0.0,
                "primary_dimension_bucket_count": 0,
                "long_strip_like_ratio": 0.0,
                "aspect_cluster_strength": 0.0,
                "is_high_discrete_random_sample": False,
                "structured_candidate_dedupe_eligible": False,
            }

        exact_size_counts: Dict[tuple[float, float], int] = {}
        primary_dimension_bucket_counts: Dict[int, int] = {}
        aspect_bucket_counts: Dict[float, int] = {}
        long_strip_like_count = 0
        for item in parts:
            min_side = float(item.get("min_side") or 0.0)
            max_side = float(item.get("max_side") or 0.0)
            aspect = float(item.get("aspect") or 0.0)
            size_key = (round(min_side, 2), round(max_side, 2))
            exact_size_counts[size_key] = exact_size_counts.get(size_key, 0) + 1
            primary_bucket = int(round(max_side / 50.0)) if max_side > 0.0 else 0
            primary_dimension_bucket_counts[primary_bucket] = primary_dimension_bucket_counts.get(primary_bucket, 0) + 1
            aspect_bucket = round(aspect * 4.0) / 4.0 if aspect > 0.0 else 0.0
            aspect_bucket_counts[aspect_bucket] = aspect_bucket_counts.get(aspect_bucket, 0) + 1
            if aspect >= 4.0:
                long_strip_like_count += 1

        repeated_piece_count = sum(count for count in exact_size_counts.values() if count > 1)
        max_size_cluster = max(exact_size_counts.values(), default=0)
        max_aspect_cluster = max(aspect_bucket_counts.values(), default=0)
        repeated_piece_ratio = repeated_piece_count / max(part_count, 1)
        size_cluster_strength = max_size_cluster / max(part_count, 1)
        aspect_cluster_strength = max_aspect_cluster / max(part_count, 1)
        primary_dimension_bucket_count = len(primary_dimension_bucket_counts)
        long_strip_like_ratio = long_strip_like_count / max(part_count, 1)
        is_high_discrete_random_sample = (
            primary_dimension_bucket_count >= self.RANDOM_PRIMARY_DIMENSION_BUCKET_COUNT_MIN
            and aspect_cluster_strength < self.RANDOM_ASPECT_CLUSTER_STRENGTH_MAX
            and repeated_piece_ratio < self.RANDOM_REPEATED_PIECE_RATIO_MAX
        )
        structured_candidate_dedupe_eligible = (
            not is_high_discrete_random_sample
            and (
                repeated_piece_ratio >= self.STRUCTURED_REPEATED_PIECE_RATIO_MIN
                or size_cluster_strength >= self.STRUCTURED_SIZE_CLUSTER_STRENGTH_MIN
            )
        )
        return {
            "part_count": int(part_count),
            "sheet_area": round(max(sheet_length * sheet_width, 0.0), 1),
            "repeated_piece_ratio": round(repeated_piece_ratio, 4),
            "size_cluster_strength": round(size_cluster_strength, 4),
            "primary_dimension_bucket_count": int(primary_dimension_bucket_count),
            "long_strip_like_ratio": round(long_strip_like_ratio, 4),
            "aspect_cluster_strength": round(aspect_cluster_strength, 4),
            "is_high_discrete_random_sample": bool(is_high_discrete_random_sample),
            "structured_candidate_dedupe_eligible": bool(structured_candidate_dedupe_eligible),
        }

    def _should_enable_pre_dedupe(
        self,
        *,
        observed_count: int,
        exact_duplicate_count: int,
        placement_count: int = 0,
    ) -> bool:
        if placement_count >= self.STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_MIN_PLACEMENTS:
            duplicate_rate = exact_duplicate_count / max(observed_count, 1)
            if (
                observed_count >= self.STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_WARMUP
                and exact_duplicate_count >= self.STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_DUPLICATES
                and duplicate_rate >= self.STRUCTURED_PRE_DEDUPE_DEEP_FAST_START_RATE
            ):
                return True
        if observed_count >= self.STRUCTURED_PRE_DEDUPE_FAST_START_WARMUP:
            duplicate_rate = exact_duplicate_count / max(observed_count, 1)
            if (
                exact_duplicate_count >= self.STRUCTURED_PRE_DEDUPE_FAST_START_DUPLICATES
                and duplicate_rate >= self.STRUCTURED_PRE_DEDUPE_FAST_START_RATE
            ):
                return True
        if observed_count < self.STRUCTURED_PRE_DEDUPE_WARMUP:
            return False
        duplicate_rate = exact_duplicate_count / max(observed_count, 1)
        return duplicate_rate >= self.STRUCTURED_PRE_DEDUPE_MIN_RATE

    def _rects_overlap(self, first: Rect, second: Rect) -> bool:
        return not (
            first.right <= second.x + EPS
            or second.right <= first.x + EPS
            or first.top <= second.y + EPS
            or second.top <= first.y + EPS
        )

    def _can_place(
        self,
        rect: Rect,
        placements: Sequence[Rect],
        sheet_length: float,
        sheet_width: float,
        *,
        overlap_candidates: Sequence[Rect] | None = None,
    ) -> bool:
        if rect.x < -EPS or rect.y < -EPS:
            return False
        if rect.right > sheet_length + EPS or rect.top > sheet_width + EPS:
            return False
        candidates = overlap_candidates if overlap_candidates is not None else placements
        return not any(self._rects_overlap(rect, other) for other in candidates)

    def _compact_placements_along_x(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
        toward_min: bool,
    ) -> List[Rect]:
        if not placements:
            return []
        compacted = [
            Rect(
                x=float(rect.x),
                y=float(rect.y),
                width=float(rect.width),
                height=float(rect.height),
                part_id=str(rect.part_id),
                part_name=str(rect.part_name),
                rotated=bool(rect.rotated),
            )
            for rect in placements
        ]
        order = (
            sorted(range(len(compacted)), key=lambda idx: (compacted[idx].x, compacted[idx].y))
            if toward_min
            else sorted(range(len(compacted)), key=lambda idx: (-compacted[idx].right, compacted[idx].y))
        )
        changed = True
        while changed:
            changed = False
            for index in order:
                rect = compacted[index]
                blockers = []
                for other_index, other in enumerate(compacted):
                    if other_index == index:
                        continue
                    y_overlap = not (rect.top <= other.y + EPS or other.top <= rect.y + EPS)
                    if not y_overlap:
                        continue
                    if toward_min:
                        if other.right <= rect.x + EPS:
                            blockers.append(other.right + self.kerf)
                    else:
                        if other.x >= rect.right - EPS:
                            blockers.append(other.x - self.kerf - rect.width)
                if toward_min:
                    new_x = max(0.0, max(blockers) if blockers else 0.0)
                    if new_x + EPS < rect.x:
                        rect.x = new_x
                        changed = True
                else:
                    limit = max(0.0, sheet_length - rect.width)
                    new_x = min(limit, min(blockers) if blockers else limit)
                    if new_x > rect.x + EPS:
                        rect.x = new_x
                        changed = True
        if all(self._can_place(rect, compacted[:idx] + compacted[idx + 1 :], sheet_length, sheet_width) for idx, rect in enumerate(compacted)):
            return compacted
        return [
            Rect(
                x=float(rect.x),
                y=float(rect.y),
                width=float(rect.width),
                height=float(rect.height),
                part_id=str(rect.part_id),
                part_name=str(rect.part_name),
                rotated=bool(rect.rotated),
            )
            for rect in placements
        ]

    def _prune_free_rects(self, free_rects: Sequence[FreeRect]) -> List[FreeRect]:
        pruned: List[FreeRect] = []
        for index, rect in enumerate(free_rects):
            contained = False
            for other_index, other in enumerate(free_rects):
                if index == other_index:
                    continue
                if (
                    rect.x >= other.x - EPS
                    and rect.y >= other.y - EPS
                    and rect.right <= other.right + EPS
                    and rect.top <= other.top + EPS
                ):
                    contained = True
                    break
            if not contained:
                pruned.append(rect)
        pruned.sort(key=lambda item: (item.y, item.x, item.width * item.height))
        return pruned

    def _guillotine_split(
        self,
        free_rect: FreeRect,
        rect: Rect,
        *,
        anchor: str,
    ) -> List[FreeRect]:
        out: List[FreeRect] = []
        cut_x = rect.right + self.kerf
        cut_y = rect.top + self.kerf
        left_cut_x = rect.x - self.kerf
        bottom_cut_y = rect.y - self.kerf

        if anchor == "bl":
            if cut_x < free_rect.right - EPS:
                out.append(FreeRect(cut_x, free_rect.y, free_rect.right - cut_x, rect.height))
            if cut_y < free_rect.top - EPS:
                out.append(FreeRect(free_rect.x, cut_y, free_rect.width, free_rect.top - cut_y))
        elif anchor == "br":
            if free_rect.x < left_cut_x - EPS:
                out.append(FreeRect(free_rect.x, free_rect.y, left_cut_x - free_rect.x, rect.height))
            if cut_y < free_rect.top - EPS:
                out.append(FreeRect(free_rect.x, cut_y, free_rect.width, free_rect.top - cut_y))
        elif anchor == "tl":
            if cut_x < free_rect.right - EPS:
                out.append(FreeRect(cut_x, rect.y, free_rect.right - cut_x, rect.height))
            if free_rect.y < bottom_cut_y - EPS:
                out.append(FreeRect(free_rect.x, free_rect.y, free_rect.width, bottom_cut_y - free_rect.y))
        elif anchor == "tr":
            if free_rect.x < left_cut_x - EPS:
                out.append(FreeRect(free_rect.x, rect.y, left_cut_x - free_rect.x, rect.height))
            if free_rect.y < bottom_cut_y - EPS:
                out.append(FreeRect(free_rect.x, free_rect.y, free_rect.width, bottom_cut_y - free_rect.y))
        return [item for item in out if item.width > EPS and item.height > EPS]

    def _update_free_rects(
        self,
        free_rects: Sequence[FreeRect],
        *,
        host_index: int,
        rect: Rect,
        anchor: str,
    ) -> List[FreeRect]:
        next_rects: List[FreeRect] = []
        for index, free_rect in enumerate(free_rects):
            if index != host_index:
                next_rects.append(free_rect)
                continue
            next_rects.extend(self._guillotine_split(free_rect, rect, anchor=anchor))
        return self._prune_free_rects(next_rects)

    def _candidate_positions(self, free_rect: FreeRect, width: float, height: float) -> List[Tuple[str, float, float]]:
        if width > free_rect.width + EPS or height > free_rect.height + EPS:
            return []
        raw = [
            ("bl", free_rect.x, free_rect.y),
            ("br", free_rect.right - width, free_rect.y),
            ("tl", free_rect.x, free_rect.top - height),
            ("tr", free_rect.right - width, free_rect.top - height),
        ]
        seen: set[tuple[str, float, float]] = set()
        out: List[Tuple[str, float, float]] = []
        for anchor, x, y in raw:
            key = (anchor, round(x, 4), round(y, 4))
            if key in seen:
                continue
            seen.add(key)
            out.append((anchor, x, y))
        return out

    def _free_rect_metrics(
        self,
        free_rects: Iterable[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        total_area = 0.0
        interior_void_area = 0.0
        skinny_area = 0.0
        reusable_area = 0.0
        edge_hugging_area = 0.0
        largest_area = 0.0
        fragment_count = 0.0
        downstream_area = 0.0
        bottom_right_area = 0.0
        edge_slit_count = 0.0
        edge_slit_penalty = 0.0
        for rect in free_rects:
            area = rect.area
            total_area += area
            largest_area = max(largest_area, area)
            fragment_count += 1.0
            min_side = min(rect.width, rect.height)
            max_side = max(rect.width, rect.height)
            aspect = max_side / max(min_side, 1.0)
            touches_edge = (
                rect.x <= EPS
                or rect.y <= EPS
                or abs(rect.right - sheet_length) <= EPS
                or abs(rect.top - sheet_width) <= EPS
            )
            touches_right = abs(rect.right - sheet_length) <= EPS
            touches_bottom = abs(rect.top - sheet_width) <= EPS
            if touches_edge:
                edge_hugging_area += area
            else:
                interior_void_area += area
            if touches_edge and min_side > self.kerf + EPS and min_side < 90.0 and max_side >= 280.0:
                edge_slit_count += 1.0
                edge_slit_penalty += (90.0 - min_side) * max_side
            if min_side < 120.0 or aspect >= 7.0:
                skinny_area += area
            if min_side >= 180.0 and aspect <= 4.5:
                reusable_area += area
            if touches_right or touches_bottom:
                downstream_area = max(downstream_area, area)
            if touches_right and touches_bottom:
                bottom_right_area = max(bottom_right_area, area)
        return {
            "total_area": total_area,
            "largest_area": largest_area,
            "interior_void_area": interior_void_area,
            "skinny_area": skinny_area,
            "reusable_area": reusable_area,
            "edge_hugging_area": edge_hugging_area,
            "fragment_count": fragment_count,
            "downstream_area": downstream_area,
            "bottom_right_area": bottom_right_area,
            "main_ratio": (largest_area / total_area) if total_area > 0 else 0.0,
            "edge_slit_count": edge_slit_count,
            "edge_slit_penalty": edge_slit_penalty,
        }

    def _candidate_free_rect_metrics(
        self,
        free_rects: Iterable[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        interior_void_area = 0.0
        skinny_area = 0.0
        fragment_count = 0.0
        edge_slit_count = 0.0
        edge_slit_penalty = 0.0
        for rect in free_rects:
            area = rect.area
            fragment_count += 1.0
            min_side = min(rect.width, rect.height)
            max_side = max(rect.width, rect.height)
            aspect = max_side / max(min_side, 1.0)
            touches_edge = (
                rect.x <= EPS
                or rect.y <= EPS
                or abs(rect.right - sheet_length) <= EPS
                or abs(rect.top - sheet_width) <= EPS
            )
            if not touches_edge:
                interior_void_area += area
            if touches_edge and min_side > self.kerf + EPS and min_side < 90.0 and max_side >= 280.0:
                edge_slit_count += 1.0
                edge_slit_penalty += (90.0 - min_side) * max_side
            if min_side < 120.0 or aspect >= 7.0:
                skinny_area += area
        return {
            "interior_void_area": interior_void_area,
            "skinny_area": skinny_area,
            "fragment_count": fragment_count,
            "edge_slit_count": edge_slit_count,
            "edge_slit_penalty": edge_slit_penalty,
        }

    def _progress_free_rect_metrics(
        self,
        free_rects: Iterable[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        largest_area = 0.0
        edge_hugging_area = 0.0
        interior_void_area = 0.0
        skinny_area = 0.0
        fragment_count = 0.0
        edge_slit_count = 0.0
        edge_slit_penalty = 0.0
        for rect in free_rects:
            area = rect.area
            largest_area = max(largest_area, area)
            fragment_count += 1.0
            min_side = min(rect.width, rect.height)
            max_side = max(rect.width, rect.height)
            aspect = max_side / max(min_side, 1.0)
            touches_edge = (
                rect.x <= EPS
                or rect.y <= EPS
                or abs(rect.right - sheet_length) <= EPS
                or abs(rect.top - sheet_width) <= EPS
            )
            if touches_edge:
                edge_hugging_area += area
            else:
                interior_void_area += area
            if touches_edge and min_side > self.kerf + EPS and min_side < 90.0 and max_side >= 280.0:
                edge_slit_count += 1.0
                edge_slit_penalty += (90.0 - min_side) * max_side
            if min_side < 120.0 or aspect >= 7.0:
                skinny_area += area
        return {
            "largest_area": largest_area,
            "edge_hugging_area": edge_hugging_area,
            "interior_void_area": interior_void_area,
            "skinny_area": skinny_area,
            "fragment_count": fragment_count,
            "edge_slit_count": edge_slit_count,
            "edge_slit_penalty": edge_slit_penalty,
        }

    def _void_cell_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        if not placements:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
                "channel_void_count": 0.0,
                "channel_void_area": 0.0,
            }

        xs = {0.0, float(sheet_length)}
        ys = {0.0, float(sheet_width)}
        for rect in placements:
            xs.add(round(rect.x, 6))
            xs.add(round(rect.right, 6))
            ys.add(round(rect.y, 6))
            ys.add(round(rect.top, 6))
        x_list = sorted(xs)
        y_list = sorted(ys)
        if len(x_list) < 2 or len(y_list) < 2:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
                "channel_void_count": 0.0,
                "channel_void_area": 0.0,
            }

        empty_cells: list[tuple[int, int, float, float, float, float]] = []
        for xi in range(len(x_list) - 1):
            for yi in range(len(y_list) - 1):
                x0 = x_list[xi]
                x1 = x_list[xi + 1]
                y0 = y_list[yi]
                y1 = y_list[yi + 1]
                if x1 - x0 <= EPS or y1 - y0 <= EPS:
                    continue
                covered = False
                for rect in placements:
                    if rect.x <= x0 + EPS and rect.right >= x1 - EPS and rect.y <= y0 + EPS and rect.top >= y1 - EPS:
                        covered = True
                        break
                if not covered:
                    empty_cells.append((xi, yi, x0, x1, y0, y1))

        if not empty_cells:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
                "channel_void_count": 0.0,
                "channel_void_area": 0.0,
            }

        cell_map = {(xi, yi): (x0, x1, y0, y1) for xi, yi, x0, x1, y0, y1 in empty_cells}
        visited: set[tuple[int, int]] = set()
        enclosed_void_count = 0.0
        enclosed_void_area = 0.0
        channel_void_count = 0.0
        channel_void_area = 0.0
        narrow_limit = max(45.0, self.kerf * 16.0)

        for start in list(cell_map):
            if start in visited:
                continue
            stack = [start]
            component: list[tuple[int, int]] = []
            touches_boundary = False
            area = 0.0
            min_span = float("inf")
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                x0, x1, y0, y1 = cell_map[node]
                area += (x1 - x0) * (y1 - y0)
                min_span = min(min_span, x1 - x0, y1 - y0)
                if node[0] == 0 or node[1] == 0 or node[0] == len(x_list) - 2 or node[1] == len(y_list) - 2:
                    touches_boundary = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbour = (node[0] + dx, node[1] + dy)
                    if neighbour in cell_map and neighbour not in visited:
                        stack.append(neighbour)
            if not touches_boundary:
                enclosed_void_count += 1.0
                enclosed_void_area += area
            elif min_span <= narrow_limit:
                channel_void_count += 1.0
                channel_void_area += area

        return {
            "enclosed_void_count": enclosed_void_count,
            "enclosed_void_area": enclosed_void_area,
            "channel_void_count": channel_void_count,
            "channel_void_area": channel_void_area,
        }

    def _enclosed_void_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        if not placements:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
            }

        xs = {0.0, float(sheet_length)}
        ys = {0.0, float(sheet_width)}
        for rect in placements:
            xs.add(round(rect.x, 6))
            xs.add(round(rect.right, 6))
            ys.add(round(rect.y, 6))
            ys.add(round(rect.top, 6))
        x_list = sorted(xs)
        y_list = sorted(ys)
        if len(x_list) < 2 or len(y_list) < 2:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
            }

        empty_cells: list[tuple[int, int, float, float, float, float]] = []
        for xi in range(len(x_list) - 1):
            for yi in range(len(y_list) - 1):
                x0 = x_list[xi]
                x1 = x_list[xi + 1]
                y0 = y_list[yi]
                y1 = y_list[yi + 1]
                if x1 - x0 <= EPS or y1 - y0 <= EPS:
                    continue
                covered = False
                for rect in placements:
                    if rect.x <= x0 + EPS and rect.right >= x1 - EPS and rect.y <= y0 + EPS and rect.top >= y1 - EPS:
                        covered = True
                        break
                if not covered:
                    empty_cells.append((xi, yi, x0, x1, y0, y1))

        if not empty_cells:
            return {
                "enclosed_void_count": 0.0,
                "enclosed_void_area": 0.0,
            }

        cell_map = {(xi, yi): (x0, x1, y0, y1) for xi, yi, x0, x1, y0, y1 in empty_cells}
        visited: set[tuple[int, int]] = set()
        enclosed_void_count = 0.0
        enclosed_void_area = 0.0

        for start in list(cell_map):
            if start in visited:
                continue
            stack = [start]
            touches_boundary = False
            area = 0.0
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                x0, x1, y0, y1 = cell_map[node]
                area += (x1 - x0) * (y1 - y0)
                if node[0] == 0 or node[1] == 0 or node[0] == len(x_list) - 2 or node[1] == len(y_list) - 2:
                    touches_boundary = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    neighbour = (node[0] + dx, node[1] + dy)
                    if neighbour in cell_map and neighbour not in visited:
                        stack.append(neighbour)
            if not touches_boundary:
                enclosed_void_count += 1.0
                enclosed_void_area += area

        return {
            "enclosed_void_count": enclosed_void_count,
            "enclosed_void_area": enclosed_void_area,
        }

    def _junction_metrics(self, placements: Sequence[Rect], *, sheet_length: float, sheet_width: float) -> Dict[str, float]:
        if len(placements) < 4:
            return {
                "cross_junction_count": 0.0,
                "cross_junction_risk": 0.0,
            }

        xs = {0.0, float(sheet_length)}
        ys = {0.0, float(sheet_width)}
        for rect in placements:
            xs.add(round(rect.x, 6))
            xs.add(round(rect.right, 6))
            ys.add(round(rect.y, 6))
            ys.add(round(rect.top, 6))
        x_list = sorted(xs)
        y_list = sorted(ys)
        if len(x_list) < 3 or len(y_list) < 3:
            return {
                "cross_junction_count": 0.0,
                "cross_junction_risk": 0.0,
            }

        owner: dict[tuple[int, int], str] = {}
        for xi in range(len(x_list) - 1):
            for yi in range(len(y_list) - 1):
                x0 = x_list[xi]
                x1 = x_list[xi + 1]
                y0 = y_list[yi]
                y1 = y_list[yi + 1]
                if x1 - x0 <= EPS or y1 - y0 <= EPS:
                    continue
                cell_owner = ""
                for rect in placements:
                    if rect.x <= x0 + EPS and rect.right >= x1 - EPS and rect.y <= y0 + EPS and rect.top >= y1 - EPS:
                        cell_owner = rect.part_id
                        break
                if cell_owner:
                    owner[(xi, yi)] = cell_owner

        cross_count = 0.0
        cross_risk = 0.0
        for xi in range(1, len(x_list) - 1):
            for yi in range(1, len(y_list) - 1):
                nw = owner.get((xi - 1, yi - 1))
                ne = owner.get((xi, yi - 1))
                sw = owner.get((xi - 1, yi))
                se = owner.get((xi, yi))
                if not (nw and ne and sw and se):
                    continue
                vertical_change = nw != ne or sw != se
                horizontal_change = nw != sw or ne != se
                if not (vertical_change and horizontal_change):
                    continue
                distinct = len({nw, ne, sw, se})
                if distinct < 3:
                    continue
                cross_count += 1.0
                min_span = min(
                    x_list[xi] - x_list[xi - 1],
                    x_list[xi + 1] - x_list[xi],
                    y_list[yi] - y_list[yi - 1],
                    y_list[yi + 1] - y_list[yi],
                )
                cross_risk += 1.0 / max(min_span, 1.0)

        return {
            "cross_junction_count": cross_count,
            "cross_junction_risk": cross_risk,
        }

    def _component_count(self, placements: Sequence[Rect]) -> int:
        if not placements:
            return 0
        remaining = set(range(len(placements)))
        count = 0
        tol = max(8.0, self.kerf * 1.5)
        while remaining:
            count += 1
            root = remaining.pop()
            stack = [root]
            while stack:
                index = stack.pop()
                rect = placements[index]
                neighbours = []
                for other_index in list(remaining):
                    other = placements[other_index]
                    overlap_x = min(rect.right, other.right) - max(rect.x, other.x)
                    overlap_y = min(rect.top, other.top) - max(rect.y, other.y)
                    touching = (
                        (overlap_y > -tol and (abs(rect.right - other.x) <= tol or abs(other.right - rect.x) <= tol))
                        or (overlap_x > -tol and (abs(rect.top - other.y) <= tol or abs(other.top - rect.y) <= tol))
                        or (overlap_x > -tol and overlap_y > -tol)
                    )
                    if touching:
                        neighbours.append(other_index)
                for other_index in neighbours:
                    remaining.remove(other_index)
                    stack.append(other_index)
        return count

    def _guillotine_metrics(self, placements: Sequence[Rect], *, sheet_length: float, sheet_width: float) -> Dict[str, int | bool]:
        rects = [
            {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
            for rect in placements
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
                            round(item["x"], 4),
                            round(item["y"], 4),
                            round(item["width"], 4),
                            round(item["height"], 4),
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
                    round(item["x"], 6)
                    for item in items
                    if x0 + EPS < item["x"] < x1 - EPS
                }
                | {
                    round(item["x"] + item["width"], 6)
                    for item in items
                    if x0 + EPS < item["x"] + item["width"] < x1 - EPS
                }
            )
            y_cuts = sorted(
                {
                    round(item["y"], 6)
                    for item in items
                    if y0 + EPS < item["y"] < y1 - EPS
                }
                | {
                    round(item["y"] + item["height"], 6)
                    for item in items
                    if y0 + EPS < item["y"] + item["height"] < y1 - EPS
                }
            )

            best_released = 0
            best_depth = 0

            for cut_x in x_cuts:
                crossing = [item for item in items if item["x"] + EPS < cut_x < item["x"] + item["width"] - EPS]
                if crossing:
                    continue
                left = [item for item in items if item["x"] + item["width"] <= cut_x + EPS]
                right = [item for item in items if item["x"] >= cut_x - EPS]
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
                crossing = [item for item in items if item["y"] + EPS < cut_y < item["y"] + item["height"] - EPS]
                if crossing:
                    continue
                bottom = [item for item in items if item["y"] + item["height"] <= cut_y + EPS]
                top = [item for item in items if item["y"] >= cut_y - EPS]
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
            "is_guillotine": is_guillotine,
            "guillotine_release_count": int(released),
            "guillotine_unreleased_count": max(0, len(rects) - int(released)),
            "guillotine_depth": int(depth),
        }

    def _fast_slicing_proxy(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        rects = [
            {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
            for rect in placements
        ]
        node_budget = max(12, len(rects) * 2)
        visited = 0
        aborted_by_depth = False

        def _cut_score(
            *,
            first_count: int,
            second_count: int,
            cut_value: float,
            span_start: float,
            span_end: float,
        ) -> float:
            imbalance = abs(first_count - second_count)
            span_bias = abs((cut_value - span_start) - (span_end - cut_value))
            edge_bias = min(abs(cut_value - span_start), abs(span_end - cut_value))
            return imbalance * 1000.0 + span_bias + edge_bias * 0.1

        def _trim_axis_cuts(cuts: list[float], span_start: float, span_end: float) -> list[float]:
            if len(cuts) <= self.FAST_SLICING_MAX_AXIS_CUTS:
                return cuts
            midpoint = (span_start + span_end) * 0.5
            ordered = sorted(cuts, key=lambda value: abs(value - midpoint))
            return sorted(ordered[: self.FAST_SLICING_MAX_AXIS_CUTS])

        def _best_cut(items: list[dict], x0: float, y0: float, x1: float, y1: float) -> tuple[str, float, list[dict], list[dict]] | None:
            best: tuple[float, str, float, list[dict], list[dict]] | None = None

            x_cuts = sorted(
                {
                    round(float(item["x"]), 6)
                    for item in items
                    if x0 + EPS < float(item["x"]) < x1 - EPS
                }
                | {
                    round(float(item["x"]) + float(item["width"]), 6)
                    for item in items
                    if x0 + EPS < float(item["x"]) + float(item["width"]) < x1 - EPS
                }
            )
            x_cuts = _trim_axis_cuts(x_cuts, x0, x1)
            for cut_x in x_cuts:
                crossing = [
                    item
                    for item in items
                    if float(item["x"]) + EPS < cut_x < float(item["x"]) + float(item["width"]) - EPS
                ]
                if crossing:
                    continue
                left = [item for item in items if float(item["x"]) + float(item["width"]) <= cut_x + EPS]
                right = [item for item in items if float(item["x"]) >= cut_x - EPS]
                if not left or not right or len(left) + len(right) != len(items):
                    continue
                score = _cut_score(
                    first_count=len(left),
                    second_count=len(right),
                    cut_value=cut_x,
                    span_start=x0,
                    span_end=x1,
                )
                candidate = (score, "x", cut_x, left, right)
                if best is None or candidate[0] < best[0]:
                    best = candidate

            y_cuts = sorted(
                {
                    round(float(item["y"]), 6)
                    for item in items
                    if y0 + EPS < float(item["y"]) < y1 - EPS
                }
                | {
                    round(float(item["y"]) + float(item["height"]), 6)
                    for item in items
                    if y0 + EPS < float(item["y"]) + float(item["height"]) < y1 - EPS
                }
            )
            y_cuts = _trim_axis_cuts(y_cuts, y0, y1)
            for cut_y in y_cuts:
                crossing = [
                    item
                    for item in items
                    if float(item["y"]) + EPS < cut_y < float(item["y"]) + float(item["height"]) - EPS
                ]
                if crossing:
                    continue
                bottom = [item for item in items if float(item["y"]) + float(item["height"]) <= cut_y + EPS]
                top = [item for item in items if float(item["y"]) >= cut_y - EPS]
                if not bottom or not top or len(bottom) + len(top) != len(items):
                    continue
                score = _cut_score(
                    first_count=len(bottom),
                    second_count=len(top),
                    cut_value=cut_y,
                    span_start=y0,
                    span_end=y1,
                )
                candidate = (score, "y", cut_y, bottom, top)
                if best is None or candidate[0] < best[0]:
                    best = candidate

            if best is None:
                return None
            return (best[1], best[2], best[3], best[4])

        def _solve(items: list[dict], x0: float, y0: float, x1: float, y1: float, depth_left: int) -> tuple[bool, int, int, int]:
            nonlocal visited, aborted_by_depth
            if len(items) <= 1:
                return (True, len(items), 0, 0)
            visited += 1
            if visited > node_budget:
                self._profile_add("fast_slicing_proxy_node_limit_abort", 1.0)
                return (False, 0, max(1, len(items)), 0)
            if depth_left <= 0:
                aborted_by_depth = True
                self._profile_add("fast_slicing_proxy_depth_abort", 1.0)
                return (False, 0, max(1, len(items)), 0)

            cut = _best_cut(items, x0, y0, x1, y1)
            if cut is None:
                return (False, 0, 1, 0)
            axis, value, first_items, second_items = cut
            if axis == "x":
                first = _solve(first_items, x0, y0, value, y1, depth_left - 1)
                second = _solve(second_items, value, y0, x1, y1, depth_left - 1)
            else:
                first = _solve(first_items, x0, y0, x1, value, depth_left - 1)
                second = _solve(second_items, x0, value, x1, y1, depth_left - 1)
            return (
                bool(first[0] and second[0]),
                int(first[1] + second[1]),
                int(first[2] + second[2]),
                1 + max(int(first[3]), int(second[3])),
            )

        slicable, released_count, blocked_region_count, depth = _solve(
            rects,
            0.0,
            0.0,
            sheet_length,
            sheet_width,
            self.FAST_SLICING_MAX_DEPTH,
        )
        unreleased_count = max(0, len(rects) - int(released_count))
        proxy_score = float(
            blocked_region_count * 1000
            + unreleased_count * 100
            + max(0, len(rects) - depth)
            + (500.0 if aborted_by_depth else 0.0)
        )
        return {
            "is_slicable": bool(slicable),
            "released_count": float(released_count),
            "unreleased_count": float(unreleased_count),
            "blocked_region_count": float(blocked_region_count),
            "depth": float(depth),
            "aborted_by_depth": 1.0 if aborted_by_depth else 0.0,
            "proxy_score": proxy_score,
        }

    def _part_relation_features(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, Any]:
        if not placements:
            return {
                "edge_touch_count": 0,
                "interior_part_count": 0,
                "rotated_count": 0,
                "adjacency_edge_count": 0,
                "vertical_group_count": 0,
                "horizontal_group_count": 0,
            }
        vertical_groups = {(round(rect.x, 4), round(rect.width, 4)) for rect in placements}
        horizontal_groups = {(round(rect.y, 4), round(rect.height, 4)) for rect in placements}
        edge_touch_count = 0
        adjacency_edge_count = 0
        for idx, rect in enumerate(placements):
            touches_edge = (
                rect.x <= EPS
                or rect.y <= EPS
                or abs(rect.right - sheet_length) <= EPS
                or abs(rect.top - sheet_width) <= EPS
            )
            if touches_edge:
                edge_touch_count += 1
            for other in placements[idx + 1 :]:
                overlap_x = min(rect.right, other.right) - max(rect.x, other.x)
                overlap_y = min(rect.top, other.top) - max(rect.y, other.y)
                touching = (
                    (overlap_y > -EPS and (abs(rect.right - other.x) <= EPS or abs(other.right - rect.x) <= EPS))
                    or (overlap_x > -EPS and (abs(rect.top - other.y) <= EPS or abs(other.top - rect.y) <= EPS))
                )
                if touching:
                    adjacency_edge_count += 1
        return {
            "edge_touch_count": int(edge_touch_count),
            "interior_part_count": int(len(placements) - edge_touch_count),
            "rotated_count": int(sum(1 for rect in placements if rect.rotated)),
            "adjacency_edge_count": int(adjacency_edge_count),
            "vertical_group_count": int(len(vertical_groups)),
            "horizontal_group_count": int(len(horizontal_groups)),
        }

    def _state_observation_summary(
        self,
        remaining: Sequence[Dict[str, Any]],
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, Any]:
        largest_free_rect_area = max((float(rect.area) for rect in free_rects), default=0.0)
        return {
            "remaining_count": int(len(remaining)),
            "free_rect_count": int(len(free_rects)),
            "largest_free_rect_area": round(largest_free_rect_area, 1),
            "fragment_count": int(len(free_rects)),
            "edge_contact_mask": {
                "left": any(rect.x <= EPS for rect in free_rects),
                "right": any(abs(rect.right - sheet_length) <= EPS for rect in free_rects),
                "bottom": any(rect.y <= EPS for rect in free_rects),
                "top": any(abs(rect.top - sheet_width) <= EPS for rect in free_rects),
            },
        }

    def _layout_equivalence_features(
        self,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, Any]:
        if not placements:
            return {
                "cluster_bbox": {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0},
                "edge_contact": {"left": False, "right": False, "top": False, "bottom": False},
                "primary_offcut_zone": "empty",
                "strip_profile": {"dominant_axis": "none", "dominant_ratio": 0.0},
                "strict_guillotine": {"is_guillotine": True, "guillotine_unreleased_count": 0, "guillotine_depth": 0},
                "fast_slicing": {"is_slicable": True, "blocked_region_count": 0, "unreleased_count": 0, "depth": 0, "aborted_by_depth": 0},
                "part_relation_features": self._part_relation_features([], sheet_length=sheet_length, sheet_width=sheet_width),
            }
        min_x = min(rect.x for rect in placements)
        min_y = min(rect.y for rect in placements)
        max_x = max(rect.right for rect in placements)
        max_y = max(rect.top for rect in placements)
        free_metrics = self._free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        strict = self._guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        slicing = self._fast_slicing_proxy(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        vertical_group_area = 0.0
        horizontal_group_area = 0.0
        vertical_groups: dict[tuple[float, float], float] = {}
        horizontal_groups: dict[tuple[float, float], float] = {}
        for rect in placements:
            area = rect.area
            vertical_groups[(round(rect.x, 4), round(rect.width, 4))] = vertical_groups.get((round(rect.x, 4), round(rect.width, 4)), 0.0) + area
            horizontal_groups[(round(rect.y, 4), round(rect.height, 4))] = horizontal_groups.get((round(rect.y, 4), round(rect.height, 4)), 0.0) + area
        vertical_group_area = max(vertical_groups.values(), default=0.0)
        horizontal_group_area = max(horizontal_groups.values(), default=0.0)
        dominant_axis = "vertical" if vertical_group_area >= horizontal_group_area else "horizontal"
        dominant_area = max(vertical_group_area, horizontal_group_area)
        total_area = sum(rect.area for rect in placements)
        primary_offcut_zone = "interior"
        if free_metrics["bottom_right_area"] > 0.0:
            primary_offcut_zone = "bottom_right"
        elif free_metrics["downstream_area"] > 0.0:
            primary_offcut_zone = "edge_downstream"
        elif free_metrics["edge_hugging_area"] > 0.0:
            primary_offcut_zone = "edge_other"
        return {
            "cluster_bbox": {
                "x": round(min_x, 2),
                "y": round(min_y, 2),
                "width": round(max_x - min_x, 2),
                "height": round(max_y - min_y, 2),
            },
            "edge_contact": {
                "left": bool(min_x <= EPS),
                "right": bool(abs(max_x - sheet_length) <= EPS),
                "top": bool(abs(max_y - sheet_width) <= EPS),
                "bottom": bool(min_y <= EPS),
            },
            "primary_offcut_zone": primary_offcut_zone,
            "strip_profile": {
                "dominant_axis": dominant_axis,
                "dominant_ratio": round((dominant_area / total_area) if total_area > 0 else 0.0, 4),
            },
            "strict_guillotine": {
                "is_guillotine": bool(strict["is_guillotine"]),
                "guillotine_unreleased_count": int(strict["guillotine_unreleased_count"]),
                "guillotine_depth": int(strict["guillotine_depth"]),
            },
            "fast_slicing": {
                "is_slicable": bool(slicing["is_slicable"]),
                "blocked_region_count": int(slicing["blocked_region_count"]),
                "unreleased_count": int(slicing["unreleased_count"]),
                "depth": int(slicing["depth"]),
                "aborted_by_depth": int(slicing["aborted_by_depth"]),
            },
            "part_relation_features": self._part_relation_features(
                placements,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            ),
        }

    def _primary_offcut_zone_from_metrics(self, free_metrics: Dict[str, float]) -> str:
        if float(free_metrics.get("bottom_right_area") or 0.0) > 0.0:
            return "bottom_right"
        if float(free_metrics.get("downstream_area") or 0.0) > 0.0:
            return "edge_downstream"
        if float(free_metrics.get("edge_hugging_area") or 0.0) > 0.0:
            return "edge_other"
        return "interior"

    def _free_rect_zone(self, rect: FreeRect, *, sheet_length: float, sheet_width: float) -> str:
        touches_right = abs(rect.right - sheet_length) <= EPS
        touches_bottom = abs(rect.top - sheet_width) <= EPS
        touches_edge = (
            rect.x <= EPS
            or rect.y <= EPS
            or touches_right
            or touches_bottom
        )
        if touches_right and touches_bottom:
            return "bottom_right"
        if touches_right or touches_bottom:
            return "edge_downstream"
        if touches_edge:
            return "edge_other"
        return "interior"

    def _build_offcut_regions(
        self,
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
        free_metrics: Dict[str, float],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        primary_zone = self._primary_offcut_zone_from_metrics(free_metrics)
        valid_rects = [rect for rect in free_rects if rect.width > EPS and rect.height > EPS and rect.area > EPS]
        if not valid_rects:
            return [], [], {
                "primary_offcut_zone": primary_zone,
                "largest_offcut_id": None,
                "largest_region_id": None,
                "reusable_offcut_ids": [],
                "reusable_region_ids": [],
                "protected_offcut_ids": [],
                "protected_region_ids": [],
                "offcut_count": 0,
                "region_count": 0,
            }

        largest_area = max(rect.area for rect in valid_rects)
        zone_target_area = {
            "bottom_right": float(free_metrics.get("bottom_right_area") or 0.0),
            "edge_downstream": float(free_metrics.get("downstream_area") or 0.0),
            "edge_other": float(free_metrics.get("edge_hugging_area") or 0.0),
            "interior": float(free_metrics.get("largest_area") or 0.0),
        }.get(primary_zone, float(free_metrics.get("largest_area") or 0.0))

        offcut_objects: List[Dict[str, Any]] = []
        for index, rect in enumerate(valid_rects, start=1):
            min_side = min(rect.width, rect.height)
            max_side = max(rect.width, rect.height)
            aspect_ratio = max_side / max(min_side, 1.0)
            zone = self._free_rect_zone(rect, sheet_length=sheet_length, sheet_width=sheet_width)
            touches = {
                "left": bool(rect.x <= EPS),
                "right": bool(abs(rect.right - sheet_length) <= EPS),
                "top": bool(rect.y <= EPS),
                "bottom": bool(abs(rect.top - sheet_width) <= EPS),
            }
            is_reusable = bool(min_side >= 180.0 and aspect_ratio <= 4.5)
            is_primary = bool(zone == primary_zone and abs(rect.area - zone_target_area) <= max(1.0, zone_target_area * 1e-6))
            shape_quality = max(0.0, min(1.0, min_side / max(max_side, 1.0)))
            protection_priority = (
                (1000000 if is_primary else 0)
                + (500000 if is_reusable else 0)
                + int(round(rect.area))
                + int(round(shape_quality * 1000.0))
            )
            offcut_objects.append(
                {
                    "id": f"offcut-{index}",
                    "x": float(rect.x),
                    "y": float(rect.y),
                    "width": float(rect.width),
                    "height": float(rect.height),
                    "area": float(rect.area),
                    "zone": zone,
                    "touches_sheet_edge": bool(any(touches.values())),
                    "touches_edges": touches,
                    "is_primary_offcut": is_primary,
                    "is_reusable": is_reusable,
                    "is_skinny": bool(min_side < 120.0 or aspect_ratio >= 7.0),
                    "aspect_ratio": round(float(aspect_ratio), 4),
                    "shape_quality": round(float(shape_quality), 4),
                    "protection_priority": int(protection_priority),
                }
            )

        adjacency: Dict[int, List[int]] = {idx: [] for idx in range(len(offcut_objects))}
        for left_index in range(len(offcut_objects)):
            for right_index in range(left_index + 1, len(offcut_objects)):
                if _rectangles_touch_or_overlap(offcut_objects[left_index], offcut_objects[right_index]):
                    adjacency[left_index].append(right_index)
                    adjacency[right_index].append(left_index)

        region_objects: List[Dict[str, Any]] = []
        visited: set[int] = set()
        for start_index in range(len(offcut_objects)):
            if start_index in visited:
                continue
            stack = [start_index]
            component: List[int] = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                stack.extend(adjacency.get(node, []))
            members = [offcut_objects[idx] for idx in component]
            min_x = min(float(item["x"]) for item in members)
            min_y = min(float(item["y"]) for item in members)
            max_x = max(float(item["x"]) + float(item["width"]) for item in members)
            max_y = max(float(item["y"]) + float(item["height"]) for item in members)
            bbox_width = max_x - min_x
            bbox_height = max_y - min_y
            bbox_area = max(1.0, bbox_width * bbox_height)
            area_total = sum(float(item["area"]) for item in members)
            largest_rect_area = max(float(item["area"]) for item in members)
            reusable_rect_count = sum(1 for item in members if bool(item["is_reusable"]))
            shape_quality = max(0.0, min(1.0, largest_rect_area / bbox_area))
            region_zone = next(
                (
                    item["zone"]
                    for item in sorted(
                        members,
                        key=lambda item: (bool(item["is_primary_offcut"]), float(item["area"])),
                        reverse=True,
                    )
                ),
                "interior",
            )
            region_is_primary = any(bool(item["is_primary_offcut"]) for item in members)
            region_protection_priority = (
                (1000000 if region_is_primary else 0)
                + (500000 if reusable_rect_count > 0 else 0)
                + int(round(area_total))
                + int(round(shape_quality * 1000.0))
            )
            region_id = f"offcut-region-{len(region_objects) + 1}"
            region_objects.append(
                {
                    "id": region_id,
                    "rect_ids": [str(item["id"]) for item in members],
                    "rects": [dict(item) for item in members],
                    "x": float(min_x),
                    "y": float(min_y),
                    "width": float(bbox_width),
                    "height": float(bbox_height),
                    "area_total": float(area_total),
                    "largest_rect_area": float(largest_rect_area),
                    "reusable_rect_count": int(reusable_rect_count),
                    "shape_quality": round(float(shape_quality), 4),
                    "fragmentation_penalty": int(max(0, len(members) - 1)),
                    "is_primary_offcut": bool(region_is_primary),
                    "zone": region_zone,
                    "protection_priority": int(region_protection_priority),
                }
            )
            for item in members:
                item["region_id"] = region_id

        ranked_region_ids = [
            item["id"]
            for item in sorted(
                region_objects,
                key=lambda item: (
                    -int(item["protection_priority"]),
                    -float(item["area_total"]),
                    -float(item["shape_quality"]),
                ),
            )
        ]
        region_index = {str(item["id"]): item for item in region_objects}
        for rank, region_id in enumerate(ranked_region_ids, start=1):
            item = region_index.get(str(region_id))
            if item is None:
                continue
            item["preserve_rank"] = int(rank)

        ranked_ids = [
            item["id"]
            for item in sorted(
                offcut_objects,
                key=lambda item: (
                    -int(item["protection_priority"]),
                    -float(item["area"]),
                    -float(item["shape_quality"]),
                ),
            )
        ]
        reusable_ids = [item["id"] for item in offcut_objects if bool(item["is_reusable"])]
        reusable_id_set = set(reusable_ids)
        offcut_index = {str(item["id"]): item for item in offcut_objects}
        for rank, offcut_id in enumerate(ranked_ids, start=1):
            item = offcut_index.get(str(offcut_id))
            if item is None:
                continue
            item["reuse_rank"] = int(rank) if offcut_id in reusable_id_set else None
            item["preserve_rank"] = int(rank)
        largest_offcut_id = next((item["id"] for item in offcut_objects if abs(float(item["area"]) - largest_area) <= 1e-6), None)
        largest_region_id = next(
            (
                item["id"]
                for item in region_objects
                if abs(float(item["largest_rect_area"]) - largest_area) <= 1e-6
            ),
            ranked_region_ids[0] if ranked_region_ids else None,
        )
        reusable_region_ids = [item["id"] for item in region_objects if int(item.get("reusable_rect_count") or 0) > 0]
        summary = {
            "primary_offcut_zone": primary_zone,
            "largest_offcut_id": largest_offcut_id,
            "largest_region_id": largest_region_id,
            "reusable_offcut_ids": reusable_ids,
            "reusable_region_ids": reusable_region_ids,
            "protected_offcut_ids": ranked_ids[: max(1, min(3, len(ranked_ids)))],
            "protected_region_ids": ranked_region_ids[: max(1, min(3, len(ranked_region_ids)))],
            "offcut_count": len(offcut_objects),
            "region_count": len(region_objects),
        }
        return region_objects, offcut_objects, summary

    def _seed_signature(
        self,
        result: SheetResult,
        *,
        input_part_count: int,
        sheet_length: float,
        sheet_width: float,
        free_metrics: Dict[str, float],
        guillotine: Dict[str, Any],
        slicing: Dict[str, Any],
    ) -> tuple[tuple[Any, ...], Dict[str, Any]]:
        if result.placements:
            min_x = min(rect.x for rect in result.placements)
            min_y = min(rect.y for rect in result.placements)
            max_x = max(rect.right for rect in result.placements)
            max_y = max(rect.top for rect in result.placements)
        else:
            min_x = 0.0
            min_y = 0.0
            max_x = 0.0
            max_y = 0.0
        edge_contact = {
            "left": bool(min_x <= EPS),
            "right": bool(abs(max_x - sheet_length) <= EPS),
            "top": bool(abs(max_y - sheet_width) <= EPS),
            "bottom": bool(min_y <= EPS),
        }
        utilization_bucket = round(float(result.utilization), 1)
        primary_offcut_zone = self._primary_offcut_zone_from_metrics(free_metrics)
        strict_summary = {
            "is_guillotine": bool(guillotine.get("is_guillotine")),
            "guillotine_unreleased_count": int(guillotine.get("guillotine_unreleased_count") or 0),
            "guillotine_depth": int(guillotine.get("guillotine_depth") or 0),
        }
        fast_summary = {
            "is_slicable": bool(slicing.get("is_slicable")),
            "blocked_region_count": int(slicing.get("blocked_region_count") or 0),
            "unreleased_count": int(slicing.get("unreleased_count") or 0),
            "depth": int(slicing.get("depth") or 0),
            "aborted_by_depth": int(slicing.get("aborted_by_depth") or 0),
        }
        cluster_bbox = {
            "x": round(min_x, 2),
            "y": round(min_y, 2),
            "width": round(max_x - min_x, 2),
            "height": round(max_y - min_y, 2),
        }
        debug = {
            "placements_count": int(len(result.placements)),
            "remaining_count": int(max(0, int(input_part_count) - len(result.placements))),
            "free_rect_count": int(len(result.free_rects)),
            "largest_free_rect_area": round(float(free_metrics.get("largest_area") or 0.0), 1),
            "fragment_count": int(free_metrics.get("fragment_count") or 0),
            "edge_contact_mask": edge_contact,
            "reusable_rect_count": int(
                sum(
                    1
                    for rect in result.free_rects
                    if min(rect.width, rect.height) >= 180.0
                    and max(rect.width, rect.height) / max(min(rect.width, rect.height), 1.0) <= 4.5
                )
            ),
            "strict_guillotine": strict_summary,
            "fast_slicing": fast_summary,
            "offcut": {
                "primary_offcut_zone": primary_offcut_zone,
                "largest_rect_area": round(float(free_metrics.get("largest_area") or 0.0), 1),
            },
            "sheet_utilization_bucket": utilization_bucket,
            "cluster_bbox": cluster_bbox,
        }
        key = (
            debug["placements_count"],
            debug["remaining_count"],
            debug["free_rect_count"],
            debug["largest_free_rect_area"],
            debug["fragment_count"],
            int(edge_contact["left"]),
            int(edge_contact["right"]),
            int(edge_contact["top"]),
            int(edge_contact["bottom"]),
            debug["reusable_rect_count"],
            strict_summary["is_guillotine"],
            strict_summary["guillotine_unreleased_count"],
            strict_summary["guillotine_depth"],
            fast_summary["is_slicable"],
            fast_summary["blocked_region_count"],
            fast_summary["unreleased_count"],
            fast_summary["depth"],
            fast_summary["aborted_by_depth"],
            primary_offcut_zone,
            utilization_bucket,
            cluster_bbox["x"],
            cluster_bbox["y"],
            cluster_bbox["width"],
            cluster_bbox["height"],
        )
        return key, debug

    def _candidate_signature(
        self,
        *,
        board_id: str,
        target: int,
        sheet_id: int,
        state_view: ChooseNextStateView,
        remaining_count_after: int,
        rect: Rect,
        after_free_rects: Sequence[FreeRect],
        free_metrics: Dict[str, float],
        slicing: Dict[str, Any],
        future_dead_count: float,
        future_fragile_count: float,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[tuple[Any, ...], Dict[str, Any]]:
        current_bbox = state_view.get_cluster_bbox()
        if current_bbox is None:
            min_x = rect.x
            min_y = rect.y
            max_x = rect.right
            max_y = rect.top
        else:
            min_x = min(current_bbox[0], rect.x)
            min_y = min(current_bbox[1], rect.y)
            max_x = max(current_bbox[2], rect.right)
            max_y = max(current_bbox[3], rect.top)
        cluster_bbox = {
            "x": round(min_x, 2),
            "y": round(min_y, 2),
            "width": round(max_x - min_x, 2),
            "height": round(max_y - min_y, 2),
        }
        edge_contact = {
            "left": bool(min_x <= EPS),
            "right": bool(abs(max_x - sheet_length) <= EPS),
            "top": bool(abs(max_y - sheet_width) <= EPS),
            "bottom": bool(min_y <= EPS),
        }
        reusable_rect_count = int(
            sum(
                1
                for free_rect in after_free_rects
                if min(free_rect.width, free_rect.height) >= 180.0
                and max(free_rect.width, free_rect.height) / max(min(free_rect.width, free_rect.height), 1.0) <= 4.5
            )
        )
        largest_free_rect_area = float(free_metrics.get("largest_area") or 0.0)
        used_area_after = sum(item.area for item in state_view.placements) + rect.area
        total_area = max(sheet_length * sheet_width, 1.0)
        top_free_rect_shapes = [
            {
                "width": round(item.width, 1),
                "height": round(item.height, 1),
                "area": round(item.area, 1),
            }
            for item in sorted(after_free_rects, key=lambda candidate: candidate.area, reverse=True)[:3]
        ]
        strict_guillotine_hint = {
            "is_guillotine_hint": bool(slicing.get("is_slicable")),
            "blocked_region_count": int(slicing.get("blocked_region_count") or 0),
            "unreleased_count": int(slicing.get("unreleased_count") or 0),
        }
        offcut_quality_bucket = {
            "primary_offcut_zone": self._primary_offcut_zone_from_metrics(free_metrics),
            "largest_rect_bucket": round(largest_free_rect_area / 1000.0, 1),
            "reusable_rect_count": reusable_rect_count,
        }
        after_host_scarcity_bucket = {
            "future_dead_count": int(future_dead_count),
            "future_fragile_count": int(future_fragile_count),
        }
        debug = {
            "board_id": board_id,
            "target": int(target),
            "sheet_id": int(sheet_id),
            "placements_count": int(len(state_view.placements) + 1),
            "remaining_count": int(max(0, remaining_count_after)),
            "free_rect_count": int(len(after_free_rects)),
            "largest_free_rect_area_bucket": round(largest_free_rect_area / 1000.0, 1),
            "fragment_count": int(free_metrics.get("fragment_count") or 0),
            "edge_contact_mask": edge_contact,
            "reusable_rect_count": reusable_rect_count,
            "strict_guillotine_hint": strict_guillotine_hint,
            "fast_slicing": {
                "is_slicable": bool(slicing.get("is_slicable")),
                "blocked_region_count": int(slicing.get("blocked_region_count") or 0),
                "unreleased_count": int(slicing.get("unreleased_count") or 0),
                "depth": int(slicing.get("depth") or 0),
                "aborted_by_depth": int(slicing.get("aborted_by_depth") or 0),
            },
            "primary_offcut_zone": offcut_quality_bucket["primary_offcut_zone"],
            "cluster_bbox": cluster_bbox,
            "top_free_rect_shapes": top_free_rect_shapes,
            "offcut_quality_bucket": offcut_quality_bucket,
            "candidate_utilization_bucket": round((used_area_after / total_area) * 100.0, 1),
            "after_host_scarcity_bucket": after_host_scarcity_bucket,
        }
        key = (
            board_id,
            int(target),
            int(sheet_id),
            debug["placements_count"],
            debug["remaining_count"],
            debug["free_rect_count"],
            debug["largest_free_rect_area_bucket"],
            debug["fragment_count"],
            int(edge_contact["left"]),
            int(edge_contact["right"]),
            int(edge_contact["top"]),
            int(edge_contact["bottom"]),
            reusable_rect_count,
            strict_guillotine_hint["is_guillotine_hint"],
            strict_guillotine_hint["blocked_region_count"],
            strict_guillotine_hint["unreleased_count"],
            bool(slicing.get("is_slicable")),
            int(slicing.get("blocked_region_count") or 0),
            int(slicing.get("unreleased_count") or 0),
            int(slicing.get("depth") or 0),
            int(slicing.get("aborted_by_depth") or 0),
            offcut_quality_bucket["primary_offcut_zone"],
            cluster_bbox["x"],
            cluster_bbox["y"],
            cluster_bbox["width"],
            cluster_bbox["height"],
        )
        return key, debug

    def _pre_candidate_signature_l1(
        self,
        *,
        state_view: ChooseNextStateView,
        remaining_count_after: int,
        rect: Rect,
        free_rect: FreeRect,
        anchor: str,
    ) -> tuple[Any, ...]:
        return (
            state_view.get_placements_signature(),
            int(max(0, remaining_count_after)),
            round(rect.x, 4),
            round(rect.y, 4),
            round(rect.width, 4),
            round(rect.height, 4),
            bool(rect.rotated),
            round(free_rect.x, 4),
            round(free_rect.y, 4),
            round(free_rect.width, 4),
            round(free_rect.height, 4),
            anchor,
        )

    def _pre_candidate_signature_l2(
        self,
        *,
        board_id: str,
        target: int,
        sheet_id: int,
        state_view: ChooseNextStateView,
        remaining_count_after: int,
        rect: Rect,
        free_rect: FreeRect,
        anchor: str,
        sheet_length: float,
        sheet_width: float,
    ) -> tuple[tuple[Any, ...], Dict[str, Any]]:
        current_bbox = state_view.get_cluster_bbox()
        if current_bbox is None:
            min_x = rect.x
            min_y = rect.y
            max_x = rect.right
            max_y = rect.top
        else:
            min_x = min(current_bbox[0], rect.x)
            min_y = min(current_bbox[1], rect.y)
            max_x = max(current_bbox[2], rect.right)
            max_y = max(current_bbox[3], rect.top)
        cluster_bbox = {
            "x": round(min_x, 2),
            "y": round(min_y, 2),
            "width": round(max_x - min_x, 2),
            "height": round(max_y - min_y, 2),
        }
        edge_contact = {
            "left": bool(min_x <= EPS),
            "right": bool(abs(max_x - sheet_length) <= EPS),
            "top": bool(abs(max_y - sheet_width) <= EPS),
            "bottom": bool(min_y <= EPS),
        }
        split_rects = self._guillotine_split(free_rect, rect, anchor=anchor)
        split_summary = tuple(
            sorted(
                (
                    round(item.width, 2),
                    round(item.height, 2),
                    round(item.area / 1000.0, 1),
                )
                for item in split_rects
            )
        )
        debug = {
            "board_id": board_id,
            "target": int(target),
            "sheet_id": int(sheet_id),
            "placements_signature_size": int(len(state_view.placements)),
            "remaining_count": int(max(0, remaining_count_after)),
            "host_anchor": anchor,
            "rect": {
                "x": round(rect.x, 2),
                "y": round(rect.y, 2),
                "width": round(rect.width, 2),
                "height": round(rect.height, 2),
                "rotated": bool(rect.rotated),
            },
            "host_rect": {
                "x": round(free_rect.x, 2),
                "y": round(free_rect.y, 2),
                "width": round(free_rect.width, 2),
                "height": round(free_rect.height, 2),
            },
            "cluster_bbox": cluster_bbox,
            "edge_contact_mask": edge_contact,
            "split_summary": [
                {"width": dims[0], "height": dims[1], "area_bucket": dims[2]}
                for dims in split_summary
            ],
        }
        key = (
            self._pre_candidate_signature_l1(
                state_view=state_view,
                remaining_count_after=remaining_count_after,
                rect=rect,
                free_rect=free_rect,
                anchor=anchor,
            ),
            int(edge_contact["left"]),
            int(edge_contact["right"]),
            int(edge_contact["top"]),
            int(edge_contact["bottom"]),
            cluster_bbox["x"],
            cluster_bbox["y"],
            cluster_bbox["width"],
            cluster_bbox["height"],
            split_summary,
        )
        return key, debug

    def _candidate_pool(
        self,
        remaining: Sequence[Dict[str, Any]],
        free_rects: Sequence[FreeRect],
        *,
        state_view: ChooseNextStateView | None = None,
    ) -> List[Dict[str, Any]]:
        started = time.perf_counter()
        if not remaining:
            return []
        sorted_hard = sorted(
            remaining,
            key=lambda item: (float(item["hardness"]), float(item["max_area"])),
            reverse=True,
        )
        pool: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in sorted_hard[: max(6, self.candidate_pool_limit // 2)]:
            if item["id"] not in seen:
                seen.add(item["id"])
                pool.append(item)

        fit_ranked: List[Tuple[float, float, Dict[str, Any]]] = []
        for item in remaining:
            stats = self._timed_fit_host_stats(item, free_rects, state_view=state_view)
            best_fill = float(stats["best_fill"])
            best_exactness = float(stats["best_slack"]) if float(stats["host_count"]) > 0.0 else float("inf")
            if best_fill > 0.0:
                fit_ranked.append((best_fill, -best_exactness, item))
        fit_ranked.sort(key=lambda entry: (entry[0], entry[1], float(entry[2]["hardness"])), reverse=True)
        for _fill, _exactness, item in fit_ranked[: self.candidate_pool_limit]:
            if item["id"] not in seen:
                seen.add(item["id"])
                pool.append(item)

        small_fillers = sorted(
            remaining,
            key=lambda item: (float(item["max_area"]), float(item["min_side"])),
        )
        for item in small_fillers[: max(4, self.candidate_pool_limit // 3)]:
            if item["id"] not in seen:
                seen.add(item["id"])
                pool.append(item)
        final_pool = pool[: self.candidate_pool_limit]
        self._profile_add("candidate_pool", (time.perf_counter() - started) * 1000.0)
        self._profile_stats["candidate_pool_size_total"] = self._profile_stats.get("candidate_pool_size_total", 0.0) + float(
            len(final_pool)
        )
        self._profile_stats["candidate_pool_size_count"] = self._profile_stats.get("candidate_pool_size_count", 0.0) + 1.0
        return final_pool

    def _fit_host_stats(
        self,
        part: Dict[str, Any],
        free_rects: Sequence[FreeRect],
    ) -> Dict[str, float]:
        host_indices: set[int] = set()
        best_fill = 0.0
        best_slack = float("inf")
        for cand in self._get_candidates(part):
            width = float(cand["width"])
            height = float(cand["height"])
            for host_index, free_rect in enumerate(free_rects):
                if width > free_rect.width + EPS or height > free_rect.height + EPS:
                    continue
                host_indices.add(host_index)
                fill = (width * height) / max(free_rect.area, 1.0)
                slack = (free_rect.width - width) + (free_rect.height - height)
                if fill > best_fill or (abs(fill - best_fill) <= EPS and slack < best_slack):
                    best_fill = fill
                    best_slack = slack
        return {
            "host_count": float(len(host_indices)),
            "best_fill": best_fill,
            "best_slack": 0.0 if best_slack == float("inf") else best_slack,
        }

    def _future_watchlist(
        self,
        remaining: Sequence[Dict[str, Any]],
        free_rects: Sequence[FreeRect],
        *,
        exclude_part_id: Optional[str],
        sheet_length: float,
        sheet_width: float,
        state_view: ChooseNextStateView | None = None,
        host_stats_cache: dict[tuple[str, tuple[tuple[float, float, float, float], ...]], Dict[str, float]] | None = None,
    ) -> List[Dict[str, float | Dict[str, Any]]]:
        started = time.perf_counter()
        if not remaining or not free_rects:
            return []
        sheet_area = max(sheet_length * sheet_width, 1.0)
        candidates: List[Dict[str, float | Dict[str, Any]]] = []
        for item in remaining:
            if exclude_part_id is not None and str(item["id"]) == exclude_part_id:
                continue
            if float(item.get("max_area") or 0.0) < sheet_area * 0.025 and float(item.get("min_side") or 0.0) < 180.0:
                continue
            stats = self._timed_fit_host_stats(item, free_rects, state_view=state_view, cache=host_stats_cache)
            host_count = stats["host_count"]
            if host_count <= 0.0:
                continue
            candidates.append(
                {
                    "part": item,
                    "host_count": host_count,
                    "best_fill": stats["best_fill"],
                    "best_slack": stats["best_slack"],
                }
            )
        if not candidates:
            return []
        candidates.sort(
            key=lambda entry: (
                float(entry["host_count"]),
                -float(cast(Dict[str, Any], entry["part"])["hardness"]),
                -float(cast(Dict[str, Any], entry["part"])["max_area"]),
                -float(entry["best_fill"]),
                float(entry["best_slack"]),
            )
        )
        fragile = [entry for entry in candidates if float(entry["host_count"]) <= 3.0]
        watch = fragile[:6] if fragile else candidates[:4]
        self._profile_add("future_watchlist", (time.perf_counter() - started) * 1000.0)
        self._profile_stats["future_watchlist_size_total"] = self._profile_stats.get("future_watchlist_size_total", 0.0) + float(
            len(watch)
        )
        self._profile_stats["future_watchlist_size_count"] = self._profile_stats.get("future_watchlist_size_count", 0.0) + 1.0
        return watch

    def _part_has_host_in_free_rects(
        self,
        part: Dict[str, Any],
        free_rects: Sequence[FreeRect],
    ) -> bool:
        for cand in self._get_candidates(part):
            width = float(cand["width"])
            height = float(cand["height"])
            for free_rect in free_rects:
                if width <= free_rect.width + EPS and height <= free_rect.height + EPS:
                    return True
        return False

    def _fresh_only_strip_dead_end(
        self,
        fresh_only_parts: Sequence[Dict[str, Any]],
        *,
        future_sheet_slots: int,
        sheet_length: float,
        sheet_width: float,
    ) -> Optional[Dict[str, float | int | str]]:
        if not fresh_only_parts or future_sheet_slots <= 0:
            return None
        full_width_required = 0.0
        full_width_parts = 0
        full_height_required = 0.0
        full_height_parts = 0
        for item in fresh_only_parts:
            candidates = self._get_candidates(item)
            if not candidates:
                continue
            if all(float(cand["width"]) >= sheet_length - EPS for cand in candidates):
                full_width_parts += 1
                full_width_required += min(float(cand["height"]) for cand in candidates)
            if all(float(cand["height"]) >= sheet_width - EPS for cand in candidates):
                full_height_parts += 1
                full_height_required += min(float(cand["width"]) for cand in candidates)
        max_total_height = float(future_sheet_slots) * float(sheet_width)
        if full_width_required > max_total_height + EPS:
            return {
                "reason": "fresh_only_full_width_strip_exceeds_future_capacity",
                "fresh_only_parts": len(fresh_only_parts),
                "strip_parts": int(full_width_parts),
                "strip_extent_required": round(full_width_required, 1),
                "strip_extent_capacity": round(max_total_height, 1),
                "future_sheet_slots": int(future_sheet_slots),
            }
        max_total_width = float(future_sheet_slots) * float(sheet_length)
        if full_height_required > max_total_width + EPS:
            return {
                "reason": "fresh_only_full_height_strip_exceeds_future_capacity",
                "fresh_only_parts": len(fresh_only_parts),
                "strip_parts": int(full_height_parts),
                "strip_extent_required": round(full_height_required, 1),
                "strip_extent_capacity": round(max_total_width, 1),
                "future_sheet_slots": int(future_sheet_slots),
            }
        return None

    def _target_dead_end_check(
        self,
        remaining: Sequence[Dict[str, Any]],
        *,
        free_rects: Sequence[FreeRect],
        future_sheet_slots: int,
        sheet_length: float,
        sheet_width: float,
    ) -> Optional[Dict[str, float | int | str]]:
        if not remaining:
            return None
        sheet_area = max(float(sheet_length) * float(sheet_width), 1.0)
        future_capacity = max(0, int(future_sheet_slots)) * sheet_area
        current_capacity = sum(rect.area for rect in free_rects)
        remaining_area = sum(float(item.get("max_area") or 0.0) for item in remaining)
        if remaining_area > current_capacity + future_capacity + EPS:
            return {
                "reason": "remaining_area_exceeds_total_capacity",
                "remaining_parts": len(remaining),
                "remaining_area": round(remaining_area, 1),
                "current_capacity": round(current_capacity, 1),
                "future_capacity": round(future_capacity, 1),
                "future_sheet_slots": int(max(0, future_sheet_slots)),
            }

        fresh_only_parts: list[Dict[str, Any]] = []
        for item in remaining:
            if not self._part_has_host_in_free_rects(item, free_rects):
                fresh_only_parts.append(item)
        if not fresh_only_parts:
            return None

        fresh_only_area = sum(float(item.get("max_area") or 0.0) for item in fresh_only_parts)
        if fresh_only_area > future_capacity + EPS:
            return {
                "reason": "fresh_only_area_exceeds_future_capacity",
                "remaining_parts": len(remaining),
                "fresh_only_parts": len(fresh_only_parts),
                "fresh_only_area": round(fresh_only_area, 1),
                "future_capacity": round(future_capacity, 1),
                "future_sheet_slots": int(max(0, future_sheet_slots)),
            }

        strip_dead_end = self._fresh_only_strip_dead_end(
            fresh_only_parts,
            future_sheet_slots=max(0, future_sheet_slots),
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        if strip_dead_end is not None:
            return {
                "remaining_parts": len(remaining),
                **strip_dead_end,
            }

        oversized_fresh_only = sum(
            1
            for item in fresh_only_parts
            if float(item.get("max_area") or 0.0) > (sheet_area * 0.5 + EPS)
        )
        if oversized_fresh_only > max(0, future_sheet_slots):
            return {
                "reason": "oversized_fresh_only_exceeds_future_slots",
                "remaining_parts": len(remaining),
                "fresh_only_parts": len(fresh_only_parts),
                "oversized_fresh_only": int(oversized_fresh_only),
                "future_sheet_slots": int(max(0, future_sheet_slots)),
            }
        return None

    def _sheet_progress_tuple(self, placements: Sequence[Rect], free_rects: Sequence[FreeRect], *, sheet_length: float, sheet_width: float) -> Tuple[float, ...]:
        used_area = sum(rect.area for rect in placements)
        free_metrics = self._timed_progress_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        components = self._timed_component_count(placements)
        return (
            void_metrics["enclosed_void_count"],
            void_metrics["enclosed_void_area"],
            -used_area,
            free_metrics["edge_slit_count"],
            free_metrics["edge_slit_penalty"],
            free_metrics["interior_void_area"],
            free_metrics["skinny_area"],
            free_metrics["fragment_count"],
            float(max(0, components - 1)),
            -free_metrics["largest_area"],
            -free_metrics["edge_hugging_area"],
        )

    def _evaluate_candidate(
        self,
        rect: Rect,
        free_rect: FreeRect,
        *,
        state_view: ChooseNextStateView,
        board_id: str,
        target: int,
        sheet_id: int,
        remaining_count_after: int,
        host_index: int,
        anchor: str,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        future_watchlist: Sequence[Dict[str, float | Dict[str, Any]]],
        sheet_length: float,
        sheet_width: float,
        host_stats_cache: dict[tuple[str, tuple[tuple[float, float, float, float], ...]], Dict[str, float]] | None = None,
    ) -> Tuple[Tuple[float, ...], List[FreeRect], tuple[Any, ...] | None, dict[str, Any] | None]:
        after_free_rects = self._timed_update_free_rects(free_rects, host_index=host_index, rect=rect, anchor=anchor)
        free_metrics = self._timed_candidate_free_rect_metrics(after_free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        after_placements = list(placements) + [rect]
        slicing_proxy = self._timed_fast_slicing_proxy(after_placements, sheet_length=sheet_length, sheet_width=sheet_width)
        attachment_started = time.perf_counter()
        gap, negative_touches, negative_shared, boundary_bias = attachment_score(
            rect,
            placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            zone_left=0.0,
            zone_bottom=0.0,
            tol=max(8.0, self.kerf * 1.5),
        )
        self._profile_add("attachment_score", (time.perf_counter() - attachment_started) * 1000.0)
        touch_count = -negative_touches
        shared_contact = -negative_shared
        island_penalty = 0.0
        if placements and touch_count <= 0.0 and boundary_bias >= 0.0:
            island_penalty = 1.0
        exactness = abs(free_rect.width - rect.width) + abs(free_rect.height - rect.height)
        fill_loss = max(0.0, free_rect.area - rect.area)
        future_dead_count = 0.0
        future_dead_hardness = 0.0
        future_host_loss = 0.0
        future_fragile_count = 0.0
        future_fragile_hardness = 0.0
        future_watch_started = time.perf_counter()
        for entry in future_watchlist:
            part = cast(Dict[str, Any], entry["part"])
            before_hosts = float(entry["host_count"])
            after_stats = self._timed_fit_host_stats(part, after_free_rects, cache=host_stats_cache)
            after_hosts = float(after_stats["host_count"])
            hardness = float(part.get("hardness") or 0.0)
            if after_hosts <= 0.0:
                future_dead_count += 1.0
                future_dead_hardness += hardness
            if after_hosts <= 1.0 and before_hosts > 1.0:
                future_fragile_count += 1.0
                future_fragile_hardness += hardness
            future_host_loss += max(0.0, before_hosts - after_hosts) * hardness
        self._profile_add("future_watchlist_after_host", (time.perf_counter() - future_watch_started) * 1000.0)
        candidate_signature_key: tuple[Any, ...] | None = None
        candidate_signature_debug: dict[str, Any] | None = None
        if self._verbose_stage_logs_enabled:
            candidate_signature_key, candidate_signature_debug = self._candidate_signature(
                board_id=board_id,
                target=target,
                sheet_id=sheet_id,
                state_view=state_view,
                remaining_count_after=remaining_count_after,
                rect=rect,
                after_free_rects=after_free_rects,
                free_metrics=free_metrics,
                slicing=slicing_proxy,
                future_dead_count=future_dead_count,
                future_fragile_count=future_fragile_count,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
        score = (
            future_dead_count,
            future_dead_hardness,
            future_fragile_count,
            future_fragile_hardness,
            future_host_loss,
            island_penalty,
            gap,
            -touch_count,
            -shared_contact,
            boundary_bias,
            free_metrics["edge_slit_count"],
            free_metrics["edge_slit_penalty"],
            free_metrics["interior_void_area"],
            free_metrics["skinny_area"],
            free_metrics["fragment_count"],
            0.0 if bool(slicing_proxy["is_slicable"]) else 1.0,
            slicing_proxy["blocked_region_count"],
            slicing_proxy["unreleased_count"],
            slicing_proxy["proxy_score"],
            fill_loss,
            exactness,
            -rect.area,
            0.0 if anchor in {"bl", "br"} else 0.5,
            rect.y,
            rect.x,
        )
        return (
            score,
            after_free_rects,
            candidate_signature_key,
            candidate_signature_debug,
        )

    def _choose_next(
        self,
        remaining: Sequence[Dict[str, Any]],
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        *,
        board_id: str,
        target: int,
        sheet_id: int,
        board_structure_profile: Dict[str, Any] | None,
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[Optional[Rect], Optional[str], List[FreeRect], Dict[str, Any]]:
        state_view = ChooseNextStateView(
            algorithm=self,
            remaining=remaining,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        pool = self._candidate_pool(remaining, free_rects, state_view=state_view)
        best_rect: Optional[Rect] = None
        best_part_id: Optional[str] = None
        best_free_rects: List[FreeRect] = []
        best_score: Optional[Tuple[float, ...]] = None
        best_debug: Dict[str, Any] = {}
        watch_cache: Dict[str, List[Dict[str, float | Dict[str, Any]]]] = {}
        started = time.perf_counter()
        evaluate_ms = 0.0
        evaluated = 0
        candidate_observed_count = 0
        seen_pre_signatures: Dict[tuple[Any, ...], Dict[str, Any]] = {}
        seen_pre_signature_l1: Dict[tuple[Any, ...], Dict[str, Any]] = {}
        seen_candidate_signatures: Dict[tuple[Any, ...], Dict[str, Any]] = {}
        exact_duplicate_hits = 0
        duplicate_best_hits = 0
        duplicate_nonwinner_hits = 0
        pre_observed_count = 0
        pre_exact_duplicate_hits = 0
        pre_duplicate_best_hits = 0
        pre_duplicate_nonwinner_hits = 0
        pre_candidate_overlap_hits = 0
        pre_candidate_mismatch_hits = 0
        candidate_dedupe_mode = "observe_only"
        candidate_dedupe_hits = 0
        candidate_dedupe_saved_evals = 0
        candidate_dedupe_saved_ms_est = 0.0
        candidate_dedupe_active_in_choose_next = False
        pre_reuse_eligible_count = 0
        pre_reuse_blocked_by_warmup_count = 0
        pre_reuse_blocked_by_duplicate_best_count = 0
        pre_reuse_blocked_by_mode_count = 0
        pre_reuse_executed_count = 0
        for part in pool:
            watchlist = watch_cache.get(str(part["id"]))
            if watchlist is None:
                watchlist = self._future_watchlist(
                    remaining,
                    free_rects,
                    exclude_part_id=str(part["id"]),
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                    state_view=state_view,
                )
                watch_cache[str(part["id"])] = watchlist
            for cand in self._get_candidates(part):
                width = float(cand["width"])
                height = float(cand["height"])
                for host_index, free_rect in enumerate(free_rects):
                    if width > free_rect.width + EPS or height > free_rect.height + EPS:
                        continue
                    for anchor, x, y in self._timed_candidate_positions(free_rect, width, height):
                        rect = Rect(
                            x=x,
                            y=y,
                            width=width,
                            height=height,
                            part_id=str(part["id"]),
                            part_name=str(part["name"]),
                            rotated=bool(cand.get("rotated", False)),
                        )
                        overlap_candidates = state_view.get_overlap_candidates(rect)
                        if not self._timed_can_place(
                            rect,
                            placements,
                            sheet_length,
                            sheet_width,
                            overlap_candidates=overlap_candidates,
                        ):
                            continue
                        remaining_count_after = max(0, len(remaining) - 1)
                        pre_l1_started = time.perf_counter() if self._verbose_stage_logs_enabled else 0.0
                        pre_l1_key = self._pre_candidate_signature_l1(
                            state_view=state_view,
                            remaining_count_after=remaining_count_after,
                            rect=rect,
                            free_rect=free_rect,
                            anchor=anchor,
                        )
                        if self._verbose_stage_logs_enabled:
                            pre_l1_elapsed = (time.perf_counter() - pre_l1_started) * 1000.0
                            self._profile_add("pre_key_l1_build", pre_l1_elapsed)
                            self._profile_add("pre_key_l1_observed", 0.0)
                        self._profile_add("pre_signature_observed", 0.0)
                        pre_observed_count += 1
                        prior_l1 = seen_pre_signature_l1.get(pre_l1_key)
                        pre_signature_key: tuple[Any, ...] | None = None
                        pre_signature_debug: Dict[str, Any] | None = None
                        prior_pre: Dict[str, Any] | None = None
                        is_pre_duplicate = False
                        if prior_l1 is not None:
                            if self._verbose_stage_logs_enabled:
                                self._profile_add("pre_key_l1_collision", 0.0)
                            prior_l1_l2_key = prior_l1.get("l2_key") if isinstance(prior_l1, dict) else None
                            if prior_l1_l2_key is None and isinstance(prior_l1, dict):
                                prior_l1_l2_key, _prior_l2_debug = self._pre_candidate_signature_l2(
                                    board_id=board_id,
                                    target=target,
                                    sheet_id=sheet_id,
                                    state_view=state_view,
                                    remaining_count_after=int(prior_l1.get("remaining_count_after") or remaining_count_after),
                                    rect=cast(Rect, prior_l1["rect"]),
                                    free_rect=cast(FreeRect, prior_l1["free_rect"]),
                                    anchor=str(prior_l1.get("anchor") or anchor),
                                    sheet_length=sheet_length,
                                    sheet_width=sheet_width,
                                )
                                prior_l1["l2_key"] = prior_l1_l2_key
                                seen_pre_signatures[prior_l1_l2_key] = {
                                    "part_id": str(prior_l1.get("part_id") or ""),
                                    "result": prior_l1.get("result"),
                                }
                            pre_l2_started = time.perf_counter() if self._verbose_stage_logs_enabled else 0.0
                            pre_signature_key, pre_signature_debug = self._pre_candidate_signature_l2(
                                board_id=board_id,
                                target=target,
                                sheet_id=sheet_id,
                                state_view=state_view,
                                remaining_count_after=remaining_count_after,
                                rect=rect,
                                free_rect=free_rect,
                                anchor=anchor,
                                sheet_length=sheet_length,
                                sheet_width=sheet_width,
                            )
                            if self._verbose_stage_logs_enabled:
                                pre_l2_elapsed = (time.perf_counter() - pre_l2_started) * 1000.0
                                self._profile_add("pre_key_l2_build", pre_l2_elapsed)
                                self._profile_add("pre_signature_build", pre_l2_elapsed)
                            prior_pre = seen_pre_signatures.get(pre_signature_key)
                            is_pre_duplicate = prior_pre is not None
                            if is_pre_duplicate:
                                if self._verbose_stage_logs_enabled:
                                    self._profile_add("pre_key_l2_exact_match", 0.0)
                        pre_duplicate_against = str(prior_pre.get("part_id")) if isinstance(prior_pre, dict) else None
                        if is_pre_duplicate:
                            pre_exact_duplicate_hits += 1
                            self._profile_add("pre_signature_exact_duplicate", 0.0)
                        else:
                            if pre_l1_key not in seen_pre_signature_l1:
                                seen_pre_signature_l1[pre_l1_key] = {
                                    "part_id": str(part["id"]),
                                    "remaining_count_after": int(remaining_count_after),
                                    "rect": rect,
                                    "free_rect": free_rect,
                                    "anchor": anchor,
                                    "result": None,
                                }
                            if pre_signature_key is not None:
                                seen_pre_signatures[pre_signature_key] = {
                                    "part_id": str(part["id"]),
                                }
                            self._profile_add("pre_signature_unique", 0.0)
                        has_pre_result = bool(is_pre_duplicate) and isinstance(prior_pre, dict) and prior_pre.get("result") is not None
                        warmup_reached = pre_observed_count >= self.STRUCTURED_PRE_DEDUPE_WARMUP
                        duplicate_rate_reached = (
                            (pre_exact_duplicate_hits / max(pre_observed_count, 1)) >= self.STRUCTURED_PRE_DEDUPE_MIN_RATE
                        )
                        if has_pre_result:
                            pre_reuse_eligible_count += 1
                            self._profile_add("pre_signature_reuse_eligible", 0.0)
                        can_reuse_pre_result = bool(
                            has_pre_result
                            and self._should_enable_pre_dedupe(
                                observed_count=pre_observed_count,
                                exact_duplicate_count=pre_exact_duplicate_hits,
                                placement_count=len(placements),
                            )
                        )
                        if has_pre_result and not warmup_reached:
                            pre_reuse_blocked_by_warmup_count += 1
                            self._profile_add("pre_signature_reuse_blocked_by_warmup", 0.0)
                        elif has_pre_result and warmup_reached and pre_duplicate_best_hits > 0:
                            pre_reuse_blocked_by_duplicate_best_count += 1
                            self._profile_add("pre_signature_reuse_blocked_by_duplicate_best", 0.0)
                        elif has_pre_result and warmup_reached and not duplicate_rate_reached:
                            pre_reuse_blocked_by_mode_count += 1
                            self._profile_add("pre_signature_reuse_blocked_by_mode", 0.0)
                        if can_reuse_pre_result:
                            candidate_dedupe_active_in_choose_next = True
                            candidate_dedupe_mode = "general_exact_dedupe"
                            cached_result = cast(Dict[str, Any], prior_pre.get("result"))
                            score = cast(Tuple[float, ...], cached_result["score"])
                            after_free_rects = [
                                FreeRect(float(item.x), float(item.y), float(item.width), float(item.height))
                                for item in cast(List[FreeRect], cached_result["after_free_rects"])
                            ]
                            candidate_signature_key = cast(tuple[Any, ...] | None, cached_result.get("candidate_signature_key"))
                            candidate_signature_debug = cast(dict[str, Any] | None, cached_result.get("candidate_signature_debug"))
                            candidate_dedupe_hits += 1
                            candidate_dedupe_saved_evals += 1
                            candidate_dedupe_saved_ms_est += float(cached_result.get("eval_ms") or 0.0)
                            self._profile_add("candidate_dedupe_hit", 0.0)
                            pre_reuse_executed_count += 1
                            self._profile_add("pre_signature_reuse_executed", 0.0)
                            self._record_eval_cost_bucket("reused_eval_ms", float(cached_result.get("eval_ms") or 0.0))
                        else:
                            eval_started = time.perf_counter()
                            score, after_free_rects, candidate_signature_key, candidate_signature_debug = self._evaluate_candidate(
                                rect,
                                free_rect,
                                state_view=state_view,
                                board_id=board_id,
                                target=target,
                                sheet_id=sheet_id,
                                remaining_count_after=max(0, len(remaining) - 1),
                                host_index=host_index,
                                anchor=anchor,
                                placements=placements,
                                free_rects=free_rects,
                                future_watchlist=watchlist,
                                sheet_length=sheet_length,
                                sheet_width=sheet_width,
                                host_stats_cache=None,
                            )
                            current_eval_ms = (time.perf_counter() - eval_started) * 1000.0
                            evaluate_ms += current_eval_ms
                            evaluated += 1
                            self._record_eval_cost_bucket("executed_eval_ms", current_eval_ms)
                            result_payload = {
                                "score": score,
                                "after_free_rects": [
                                    FreeRect(float(item.x), float(item.y), float(item.width), float(item.height))
                                    for item in after_free_rects
                                ],
                                "candidate_signature_key": candidate_signature_key,
                                "candidate_signature_debug": candidate_signature_debug,
                                "eval_ms": current_eval_ms,
                            }
                            l1_entry = seen_pre_signature_l1.get(pre_l1_key)
                            if isinstance(l1_entry, dict):
                                l1_entry["result"] = result_payload
                            if pre_signature_key is not None and isinstance(seen_pre_signatures.get(pre_signature_key), dict):
                                seen_pre_signatures[pre_signature_key]["result"] = result_payload
                        prior_candidate = None
                        is_duplicate_candidate = False
                        duplicate_against = None
                        if self._verbose_stage_logs_enabled and candidate_signature_key is not None:
                            self._profile_add("candidate_signature_observed", 0.0)
                            candidate_observed_count += 1
                            prior_candidate = seen_candidate_signatures.get(candidate_signature_key)
                            is_duplicate_candidate = prior_candidate is not None
                            duplicate_against = str(prior_candidate.get("part_id")) if isinstance(prior_candidate, dict) else None
                            if is_pre_duplicate == is_duplicate_candidate:
                                if is_pre_duplicate:
                                    pre_candidate_overlap_hits += 1
                                    self._profile_add("pre_signature_candidate_exact_overlap", 0.0)
                            else:
                                pre_candidate_mismatch_hits += 1
                                self._profile_add("pre_signature_candidate_mismatch", 0.0)
                            if is_duplicate_candidate:
                                exact_duplicate_hits += 1
                                self._profile_add("candidate_signature_exact_duplicate", 0.0)
                        if best_score is None or score < best_score:
                            best_score = score
                            best_rect = rect
                            best_part_id = str(part["id"])
                            best_free_rects = after_free_rects
                            if is_pre_duplicate:
                                pre_duplicate_best_hits += 1
                                self._profile_add("pre_signature_duplicate_best_candidate", 0.0)
                            if is_duplicate_candidate:
                                duplicate_best_hits += 1
                                self._profile_add("candidate_signature_duplicate_best_candidate", 0.0)
                            best_debug = {
                                "score": score,
                                "anchor": anchor,
                                "host_free_rect": {
                                    "x": free_rect.x,
                                    "y": free_rect.y,
                                    "width": free_rect.width,
                                    "height": free_rect.height,
                                },
                                "state_observation": self._state_observation_summary(
                                    remaining,
                                    free_rects,
                                    sheet_length=sheet_length,
                                    sheet_width=sheet_width,
                                ) if self._verbose_stage_logs_enabled else None,
                                "candidate_signature_key": "|".join(str(item) for item in candidate_signature_key) if self._verbose_stage_logs_enabled and candidate_signature_key is not None else None,
                                "candidate_signature_debug": candidate_signature_debug if self._verbose_stage_logs_enabled else None,
                                "candidate_duplicate": bool(is_duplicate_candidate),
                                "candidate_duplicate_against": duplicate_against,
                                "pre_signature_key": "|".join(str(item) for item in pre_signature_key) if self._verbose_stage_logs_enabled and pre_signature_key is not None else None,
                                "pre_signature_debug": pre_signature_debug if self._verbose_stage_logs_enabled else None,
                                "pre_signature_duplicate": bool(is_pre_duplicate),
                                "pre_signature_duplicate_against": pre_duplicate_against,
                            }
                        else:
                            if is_pre_duplicate:
                                pre_duplicate_nonwinner_hits += 1
                                self._profile_add("pre_signature_duplicate_nonwinner", 0.0)
                        if self._verbose_stage_logs_enabled and is_duplicate_candidate and not (best_score is None or score < best_score):
                            duplicate_nonwinner_hits += 1
                            self._profile_add("candidate_signature_duplicate_nonwinner", 0.0)
                        if self._verbose_stage_logs_enabled and candidate_signature_key is not None and prior_candidate is None:
                            seen_candidate_signatures[candidate_signature_key] = {
                                "part_id": str(part["id"]),
                                "score": score,
                            }
                            self._profile_add("candidate_signature_unique", 0.0)
        self._profile_add("choose_next", (time.perf_counter() - started) * 1000.0)
        if evaluate_ms > 0.0:
            self._profile_add("evaluate_candidate", evaluate_ms, count=evaluated)
        if candidate_dedupe_active_in_choose_next:
            self._profile_stats["candidate_dedupe_active_choose_next_count"] = (
                self._profile_stats.get("candidate_dedupe_active_choose_next_count", 0.0) + 1.0
            )
        self._profile_stats["candidate_dedupe_saved_evals"] = (
            self._profile_stats.get("candidate_dedupe_saved_evals", 0.0) + float(candidate_dedupe_saved_evals)
        )
        self._profile_stats["candidate_dedupe_saved_ms_est"] = (
            self._profile_stats.get("candidate_dedupe_saved_ms_est", 0.0) + float(candidate_dedupe_saved_ms_est)
        )
        if self._verbose_stage_logs_enabled:
            best_debug["candidate_duplicate_summary"] = {
                "observed_count": candidate_observed_count,
                "exact_duplicate_count": exact_duplicate_hits,
                "unique_count": max(candidate_observed_count - exact_duplicate_hits, 0),
                "duplicate_best_candidate_count": duplicate_best_hits,
                "duplicate_nonwinner_count": duplicate_nonwinner_hits,
            }
            best_debug["pre_signature_duplicate_summary"] = {
                "observed_count": pre_observed_count,
                "exact_duplicate_count": pre_exact_duplicate_hits,
                "unique_count": max(pre_observed_count - pre_exact_duplicate_hits, 0),
                "duplicate_best_candidate_count": pre_duplicate_best_hits,
                "duplicate_nonwinner_count": pre_duplicate_nonwinner_hits,
                "candidate_exact_overlap_count": pre_candidate_overlap_hits,
                "candidate_mismatch_count": pre_candidate_mismatch_hits,
            }
        best_debug["candidate_dedupe"] = {
            "mode": candidate_dedupe_mode,
            "hits": int(candidate_dedupe_hits),
            "saved_evals": int(candidate_dedupe_saved_evals),
            "saved_ms_est": round(candidate_dedupe_saved_ms_est, 1),
        }
        best_debug["pre_signature_reuse_funnel"] = {
            "eligible_count": int(pre_reuse_eligible_count),
            "blocked_by_warmup_count": int(pre_reuse_blocked_by_warmup_count),
            "blocked_by_duplicate_best_count": int(pre_reuse_blocked_by_duplicate_best_count),
            "blocked_by_mode_count": int(pre_reuse_blocked_by_mode_count),
            "executed_count": int(pre_reuse_executed_count),
        }
        return best_rect, best_part_id, best_free_rects, best_debug

    def _seed_options(
        self,
        remaining: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> List[Tuple[str, Optional[str], Optional[Dict[str, Any]]]]:
        options: List[Tuple[str, Optional[str], Optional[Dict[str, Any]]]] = [("auto", None, None)]
        seen: set[str] = set()
        for part in remaining[: self.seed_limit]:
            for cand in self._get_candidates(part):
                width = float(cand["width"])
                height = float(cand["height"])
                if width > sheet_length + EPS or height > sheet_width + EPS:
                    continue
                key = f"{part['id']}:{round(width, 3)}:{round(height, 3)}"
                if key in seen:
                    continue
                seen.add(key)
                options.append((f"seed:{part['id']}", str(part["id"]), cand))
        return options

    def _cleanup_score(
        self,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[float, ...]:
        guillotine = self._timed_guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        free_metrics = self._timed_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        used_area = sum(rect.area for rect in placements)
        return (
            float(void_metrics["enclosed_void_count"]),
            float(void_metrics["enclosed_void_area"]),
            0.0 if bool(guillotine["is_guillotine"]) else 1.0,
            float(guillotine["guillotine_unreleased_count"]),
            float(free_metrics["edge_slit_count"]),
            float(free_metrics["edge_slit_penalty"]),
            -float(free_metrics["bottom_right_area"]),
            -float(free_metrics["downstream_area"]),
            -float(free_metrics["main_ratio"]),
            -float(free_metrics["largest_area"]),
            -float(free_metrics["reusable_area"]),
            float(free_metrics["skinny_area"]),
            float(free_metrics["fragment_count"]),
            -used_area,
        )

    def _offcut_shaping_score(
        self,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[float, ...]:
        guillotine = self._timed_guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        free_metrics = self._timed_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        cluster_metrics = self._cluster_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        used_area = sum(rect.area for rect in placements)
        components = self._timed_component_count(placements)
        return (
            float(void_metrics["enclosed_void_count"]),
            float(void_metrics["enclosed_void_area"]),
            0.0 if bool(guillotine["is_guillotine"]) else 1.0,
            float(guillotine["guillotine_unreleased_count"]),
            float(max(0, components - 1)),
            float(cluster_metrics["bbox_area"]),
            -float(cluster_metrics["fill_ratio"]),
            float(cluster_metrics["aspect_penalty"]),
            float(cluster_metrics["corner_distance"]),
            float(free_metrics["interior_void_area"]),
            -float(free_metrics["reusable_area"]),
            -float(free_metrics["largest_area"]),
            -float(free_metrics["downstream_area"]),
            -float(free_metrics["bottom_right_area"]),
            float(free_metrics["fragment_count"]),
            float(free_metrics["skinny_area"]),
            -used_area,
        )

    def _single_sheet_offcut_score(
        self,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[float, ...]:
        guillotine = self._timed_guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        free_metrics = self._timed_free_rect_metrics(free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        cluster_metrics = self._cluster_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        used_area = sum(rect.area for rect in placements)
        components = self._timed_component_count(placements)
        return (
            float(void_metrics["enclosed_void_count"]),
            float(void_metrics["enclosed_void_area"]),
            0.0 if bool(guillotine["is_guillotine"]) else 1.0,
            float(guillotine["guillotine_unreleased_count"]),
            float(max(0, components - 1)),
            -float(free_metrics["reusable_area"]),
            -float(free_metrics["largest_area"]),
            -float(free_metrics["downstream_area"]),
            -float(free_metrics["bottom_right_area"]),
            float(free_metrics["fragment_count"]),
            float(free_metrics["skinny_area"]),
            float(cluster_metrics["corner_distance"]),
            float(cluster_metrics["bbox_area"]),
            -float(cluster_metrics["fill_ratio"]),
            float(cluster_metrics["aspect_penalty"]),
            -used_area,
        )

    def _cluster_metrics(
        self,
        placements: Sequence[Rect],
        *,
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, float]:
        if not placements:
            return {
                "bbox_area": 0.0,
                "bbox_width": 0.0,
                "bbox_height": 0.0,
                "fill_ratio": 0.0,
                "aspect_penalty": 0.0,
                "corner_distance": 0.0,
            }
        min_x = min(rect.x for rect in placements)
        min_y = min(rect.y for rect in placements)
        max_x = max(rect.right for rect in placements)
        max_y = max(rect.top for rect in placements)
        bbox_width = max_x - min_x
        bbox_height = max_y - min_y
        bbox_area = max(bbox_width * bbox_height, 0.0)
        used_area = sum(rect.area for rect in placements)
        fill_ratio = (used_area / bbox_area) if bbox_area > EPS else 0.0
        min_side = max(min(bbox_width, bbox_height), 1.0)
        max_side = max(bbox_width, bbox_height)
        aspect_penalty = abs((max_side / min_side) - 1.0)
        corner_distance = min(
            min_x + min_y,
            min_x + max(0.0, sheet_width - max_y),
            max(0.0, sheet_length - max_x) + min_y,
            max(0.0, sheet_length - max_x) + max(0.0, sheet_width - max_y),
        )
        return {
            "bbox_area": bbox_area,
            "bbox_width": bbox_width,
            "bbox_height": bbox_height,
            "fill_ratio": fill_ratio,
            "aspect_penalty": aspect_penalty,
            "corner_distance": corner_distance,
        }

    def _run_seeded_layout(
        self,
        remaining: Sequence[Dict[str, Any]],
        *,
        board_id: str,
        target: int,
        sheet_id: int,
        board_structure_profile: Dict[str, Any] | None,
        sheet_length: float,
        sheet_width: float,
        seed_part_id: Optional[str],
        seed_candidate: Optional[Dict[str, Any]],
        future_sheet_slots: int = 0,
        source_strategy: str = "seeded-layout",
    ) -> SheetResult:
        placements: List[Rect] = []
        free_rects: List[FreeRect] = [FreeRect(0.0, 0.0, sheet_length, sheet_width)]
        remaining_parts = list(remaining)
        debug_steps: List[Dict[str, Any]] = []
        started = time.perf_counter()
        choose_next_ms = 0.0
        choose_iterations = 0

        if seed_part_id and seed_candidate:
            seed_part = next((item for item in remaining_parts if str(item["id"]) == seed_part_id), None)
            if seed_part is not None:
                seed_rect = Rect(
                    x=0.0,
                    y=0.0,
                    width=float(seed_candidate["width"]),
                    height=float(seed_candidate["height"]),
                    part_id=str(seed_part["id"]),
                    part_name=str(seed_part["name"]),
                    rotated=bool(seed_candidate.get("rotated", False)),
                )
                if self._can_place(seed_rect, placements, sheet_length, sheet_width):
                    placements.append(seed_rect)
                    free_rects = self._update_free_rects(
                        free_rects,
                        host_index=0,
                        rect=seed_rect,
                        anchor="bl",
                    )
                    remaining_parts = [item for item in remaining_parts if str(item["id"]) != seed_part_id]
                    debug_steps.append(
                        {
                            "action": "seed",
                            "part_id": seed_part_id,
                            "anchor": "bl",
                            "host_free_rect": {"x": 0.0, "y": 0.0, "width": float(sheet_length), "height": float(sheet_width)},
                        }
                    )

        stagnant_rounds = 0
        while remaining_parts and free_rects:
            choose_started = time.perf_counter()
            next_rect, part_id, next_free_rects, candidate_debug = self._choose_next(
                remaining_parts,
                placements,
                free_rects,
                board_id=board_id,
                target=target,
                sheet_id=sheet_id,
                board_structure_profile=board_structure_profile,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            choose_next_ms += (time.perf_counter() - choose_started) * 1000.0
            choose_iterations += 1
            if next_rect is None or part_id is None:
                break
            placements.append(next_rect)
            free_rects = next_free_rects
            remaining_parts = [item for item in remaining_parts if str(item["id"]) != part_id]
            debug_steps.append({"action": "place", "part_id": part_id, **candidate_debug})

            stagnant_rounds = 0
            dead_end = self._target_dead_end_check(
                remaining_parts,
                free_rects=free_rects,
                future_sheet_slots=future_sheet_slots,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            if dead_end is not None:
                debug_steps.append({"action": "target_dead_end", **dead_end})
                break

            if stagnant_rounds >= 2:
                break

        guillotine = self._timed_guillotine_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        junction_metrics = self._timed_junction_metrics(placements, sheet_length=sheet_length, sheet_width=sheet_width)
        total_ms = (time.perf_counter() - started) * 1000.0
        self._profile_add("seed_layout", total_ms)
        self._profile_log(
            "seed_layout",
            seed=(seed_part_id or "auto"),
            dt_ms=total_ms,
            choose_next_ms=choose_next_ms,
            choose_iterations=choose_iterations,
            placements=len(placements),
            remaining=len(remaining_parts),
        )

        return SheetResult(
            id=0,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            debug={
                "cut_plan_source": source_strategy,
                "seed_part_id": seed_part_id,
                "step_count": len(debug_steps),
                "steps": debug_steps,
                "guillotine": guillotine,
                "void_metrics": void_metrics,
                "junction_metrics": junction_metrics,
                "score": self._sheet_progress_tuple(
                    placements,
                    free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                ),
            },
        )

    def _cleanup_sheet(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        board_id: str,
        target: int,
        board_structure_profile: Dict[str, Any] | None,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
    ) -> Optional[SheetResult]:
        if not parts:
            return None
        target_count = len(parts)
        best: Optional[SheetResult] = None
        best_score: Optional[Tuple[float, ...]] = None
        for _label, seed_part_id, seed_candidate in self._seed_options(
            parts,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        ):
            result = self._run_seeded_layout(
                parts,
                board_id=board_id,
                target=target,
                sheet_id=sheet_id,
                board_structure_profile=board_structure_profile,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                seed_part_id=seed_part_id,
                seed_candidate=seed_candidate,
                future_sheet_slots=0,
                source_strategy="cleanup-seeded",
            )
            if len(result.placements) != target_count:
                continue
            score = self._cleanup_score(
                result.placements,
                result.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            if best_score is None or score < best_score:
                best = result
                best_score = score
        if best is None:
            return None
        best.id = sheet_id
        return best

    def _cleanup_candidate_score(
        self,
        rect: Rect,
        free_rect: FreeRect,
        *,
        host_index: int,
        anchor: str,
        placements: Sequence[Rect],
        free_rects: Sequence[FreeRect],
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[float, ...]:
        after_free_rects = self._update_free_rects(free_rects, host_index=host_index, rect=rect, anchor=anchor)
        after_placements = list(placements) + [rect]
        free_metrics = self._timed_free_rect_metrics(after_free_rects, sheet_length=sheet_length, sheet_width=sheet_width)
        void_metrics = self._timed_enclosed_void_metrics(after_placements, sheet_length=sheet_length, sheet_width=sheet_width)
        gap, negative_touches, negative_shared, boundary_bias = attachment_score(
            rect,
            placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            zone_left=0.0,
            zone_bottom=0.0,
            tol=max(8.0, self.kerf * 1.5),
        )
        touch_count = -negative_touches
        shared_contact = -negative_shared
        return (
            void_metrics["enclosed_void_count"],
            void_metrics["enclosed_void_area"],
            free_metrics["edge_slit_count"],
            free_metrics["edge_slit_penalty"],
            -free_metrics["bottom_right_area"],
            -free_metrics["downstream_area"],
            -free_metrics["main_ratio"],
            -free_metrics["largest_area"],
            -free_metrics["reusable_area"],
            free_metrics["skinny_area"],
            free_metrics["fragment_count"],
            gap,
            -touch_count,
            -shared_contact,
            boundary_bias,
            -rect.area,
            0.0 if anchor in {"br", "tr"} else 0.5,
            rect.y,
            rect.x,
        )

    def _run_cleanup_layout(
        self,
        remaining: Sequence[Dict[str, Any]],
        *,
        sheet_id: int,
        sheet_length: float,
        sheet_width: float,
        seed_part_id: Optional[str],
        seed_candidate: Optional[Dict[str, Any]],
    ) -> SheetResult:
        placements: List[Rect] = []
        free_rects: List[FreeRect] = [FreeRect(0.0, 0.0, sheet_length, sheet_width)]
        remaining_parts = list(remaining)
        debug_steps: List[Dict[str, Any]] = []

        if seed_part_id and seed_candidate:
            seed_part = next((item for item in remaining_parts if str(item["id"]) == seed_part_id), None)
            if seed_part is not None:
                seed_rect = Rect(
                    x=sheet_length - float(seed_candidate["width"]),
                    y=sheet_width - float(seed_candidate["height"]),
                    width=float(seed_candidate["width"]),
                    height=float(seed_candidate["height"]),
                    part_id=str(seed_part["id"]),
                    part_name=str(seed_part["name"]),
                    rotated=bool(seed_candidate.get("rotated", False)),
                )
                if self._can_place(seed_rect, placements, sheet_length, sheet_width):
                    placements.append(seed_rect)
                    free_rects = self._update_free_rects(
                        free_rects,
                        host_index=0,
                        rect=seed_rect,
                        anchor="tr",
                    )
                    remaining_parts = [item for item in remaining_parts if str(item["id"]) != seed_part_id]
                    debug_steps.append(
                        {
                            "action": "cleanup-seed",
                            "part_id": seed_part_id,
                            "anchor": "tr",
                            "host_free_rect": {"x": 0.0, "y": 0.0, "width": float(sheet_length), "height": float(sheet_width)},
                        }
                    )

        while remaining_parts and free_rects:
            pool = self._candidate_pool(remaining_parts, free_rects)
            best_rect: Optional[Rect] = None
            best_part_id: Optional[str] = None
            best_free_rects: List[FreeRect] = []
            best_score: Optional[Tuple[float, ...]] = None
            best_host_free_rect: Optional[FreeRect] = None
            best_anchor: Optional[str] = None
            for part in pool:
                for cand in self._get_candidates(part):
                    width = float(cand["width"])
                    height = float(cand["height"])
                    for host_index, free_rect in enumerate(free_rects):
                        if width > free_rect.width + EPS or height > free_rect.height + EPS:
                            continue
                        for anchor, x, y in self._candidate_positions(free_rect, width, height):
                            rect = Rect(
                                x=x,
                                y=y,
                                width=width,
                                height=height,
                                part_id=str(part["id"]),
                                part_name=str(part["name"]),
                                rotated=bool(cand.get("rotated", False)),
                            )
                            if not self._can_place(rect, placements, sheet_length, sheet_width):
                                continue
                            score = self._cleanup_candidate_score(
                                rect,
                                free_rect,
                                host_index=host_index,
                                anchor=anchor,
                                placements=placements,
                                free_rects=free_rects,
                                sheet_length=sheet_length,
                                sheet_width=sheet_width,
                            )
                            if best_score is None or score < best_score:
                                best_score = score
                                best_rect = rect
                                best_part_id = str(part["id"])
                                best_free_rects = self._update_free_rects(
                                    free_rects,
                                    host_index=host_index,
                                    rect=rect,
                                    anchor=anchor,
                                )
                                best_host_free_rect = free_rect
                                best_anchor = anchor
            if best_rect is None or best_part_id is None:
                break
            placements.append(best_rect)
            free_rects = best_free_rects
            remaining_parts = [item for item in remaining_parts if str(item["id"]) != best_part_id]
            debug_step: Dict[str, Any] = {"action": "cleanup-place", "part_id": best_part_id}
            if best_host_free_rect is not None:
                debug_step["host_free_rect"] = _rect_payload(best_host_free_rect)
            if best_anchor is not None:
                debug_step["anchor"] = str(best_anchor)
            debug_steps.append(debug_step)

        return SheetResult(
            id=sheet_id,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            debug={
                "cut_plan_source": "cleanup-layout",
                "cleanup_steps": len(debug_steps),
                "steps": debug_steps,
            },
        )

    def _run_single_sheet_shelf_cleanup(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
    ) -> Optional[SheetResult]:
        if not parts:
            return None
        from grainline._internal.optimization import ShelfAlgorithm, _compute_leftovers_from_placements

        prepared = [
            {
                "id": str(part["id"]),
                "name": str(part["name"]),
                "width": float(part.get("max_side") or 0.0),
                "height": float(part.get("min_side") or 0.0),
                "candidates": part.get("candidates"),
            }
            for part in parts
        ]
        shelf = ShelfAlgorithm(kerf_mm=self.kerf, allow_rotation=self.allow_rotation)
        placements_raw, unplaced = shelf.optimize_sheet(prepared, sheet_length, sheet_width)
        if unplaced:
            return None
        placements = [
            Rect(
                x=float(rect.x),
                y=float(rect.y),
                width=float(rect.width),
                height=float(rect.height),
                part_id=str(rect.part_id),
                part_name=str(rect.part_name),
                rotated=bool(rect.rotated),
            )
            for rect in placements_raw
        ]
        leftovers = _compute_leftovers_from_placements(
            [
                {
                    "part_id": rect.part_id,
                    "part_name": rect.part_name,
                    "x": rect.x,
                    "y": rect.y,
                    "width": rect.width,
                    "height": rect.height,
                    "rotated": rect.rotated,
                }
                for rect in placements
            ],
            sheet_length=float(sheet_length),
            sheet_width=float(sheet_width),
        )
        free_rects = [
            FreeRect(
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item["width"]),
                height=float(item["height"]),
            )
            for item in leftovers
            if float(item.get("width") or 0.0) > EPS and float(item.get("height") or 0.0) > EPS
        ]
        return SheetResult(
            id=sheet_id,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            debug={"cleanup_strategy": "single-sheet-shelf"},
        )

    def _preferred_row_candidates(self, part: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates = self._get_candidates(part)
        ranked = sorted(
            candidates,
            key=lambda cand: (
                -float(cand["height"]),
                float(cand["width"]),
                -float(cand["width"]) * float(cand["height"]),
            ),
        )
        return ranked[:2]

    def _row_choice_variants(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        target_span: float,
    ) -> List[List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        if not parts:
            return []
        variants: List[List[Tuple[Dict[str, Any], Dict[str, Any]]]] = []

        def walk(index: int, current: List[Tuple[Dict[str, Any], Dict[str, Any]]], used_span: float) -> None:
            if index >= len(parts):
                if current:
                    variants.append(list(current))
                return
            part = parts[index]
            for cand in self._preferred_row_candidates(part):
                next_span = used_span + (self.kerf if current else 0.0) + float(cand["height"])
                if next_span > target_span + EPS:
                    continue
                current.append((part, cand))
                walk(index + 1, current, next_span)
                current.pop()

        walk(0, [], 0.0)
        variants.sort(
            key=lambda items: (
                max(float(cand["width"]) for _, cand in items),
                -sum(float(cand["height"]) for _, cand in items),
            )
        )
        return variants

    def _choose_row_subset(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        target_span: float,
        min_items: int,
        prefer_large_depth: bool = True,
        max_items: Optional[int] = None,
    ) -> Optional[List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        if not parts:
            return None
        best: Optional[List[Tuple[Dict[str, Any], Dict[str, Any]]]] = None
        best_score: Optional[Tuple[float, ...]] = None
        total_masks = 1 << len(parts)
        for mask in range(1, total_masks):
            subset = [parts[idx] for idx in range(len(parts)) if mask & (1 << idx)]
            if len(subset) < min_items:
                continue
            if max_items is not None and len(subset) > max_items:
                continue
            for choice in self._row_choice_variants(subset, target_span=target_span):
                row_span = sum(float(cand["height"]) for _, cand in choice) + self.kerf * max(0, len(choice) - 1)
                row_depth = max(float(cand["width"]) for _, cand in choice)
                if row_span > target_span + EPS:
                    continue
                span_gap = target_span - row_span
                mixed_depth = sum((row_depth - float(cand["width"])) * float(cand["height"]) for _, cand in choice)
                score = (
                    mixed_depth,
                    -row_depth if prefer_large_depth else row_depth,
                    0.0 if span_gap <= 180.0 else 1.0,
                    span_gap,
                    float(len(choice)),
                    -row_span,
                )
                if best_score is None or score < best_score:
                    best = choice
                    best_score = score
        return best

    def _row_subset_options(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        target_span: float,
        min_items: int,
        prefer_large_depth: bool = True,
        max_items: Optional[int] = None,
        limit: int = 12,
    ) -> List[List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        options: List[Tuple[Tuple[float, ...], List[Tuple[Dict[str, Any], Dict[str, Any]]]]] = []
        total_masks = 1 << len(parts)
        for mask in range(1, total_masks):
            subset = [parts[idx] for idx in range(len(parts)) if mask & (1 << idx)]
            if len(subset) < min_items:
                continue
            if max_items is not None and len(subset) > max_items:
                continue
            for choice in self._row_choice_variants(subset, target_span=target_span):
                row_span = sum(float(cand["height"]) for _, cand in choice) + self.kerf * max(0, len(choice) - 1)
                row_depth = max(float(cand["width"]) for _, cand in choice)
                if row_span > target_span + EPS:
                    continue
                span_gap = target_span - row_span
                mixed_depth = sum((row_depth - float(cand["width"])) * float(cand["height"]) for _, cand in choice)
                score = (
                    mixed_depth,
                    -row_depth if prefer_large_depth else row_depth,
                    0.0 if span_gap <= 180.0 else 1.0,
                    span_gap,
                    float(len(choice)),
                    -row_span,
                )
                options.append((score, choice))
        options.sort(key=lambda item: item[0])
        return [choice for _, choice in options[:limit]]

    def _run_single_sheet_strip_cleanup(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
    ) -> Optional[SheetResult]:
        if not parts:
            return None
        from grainline._internal.optimization import _compute_leftovers_from_placements

        variants: List[SheetResult] = []
        def _band_span(band: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]]) -> float:
            return sum(float(cand["height"]) for _, cand in band) + self.kerf * max(0, len(band) - 1)

        min_first_span = max(sheet_width * 0.82, sheet_width - 180.0)
        first_row_options = self._row_subset_options(
            parts,
            target_span=sheet_width,
            min_items=1,
            max_items=2,
            prefer_large_depth=True,
        )
        first_row_options = [choice for choice in first_row_options if _band_span(choice) >= min_first_span]
        if not first_row_options:
            first_row_options = self._row_subset_options(
                parts,
                target_span=sheet_width,
                min_items=1,
                max_items=2,
                prefer_large_depth=True,
                limit=6,
            )
        if not first_row_options:
            return None
        for stack_from_left in (True, False):
            for span_from_bottom in (True, False):
                for first_row in first_row_options:
                    first_ids = {str(part["id"]) for part, _ in first_row}
                    remaining_after_first = [part for part in parts if str(part["id"]) not in first_ids]
                    second_row_candidates = [[]]
                    if remaining_after_first:
                        raw_second_row_candidates = self._row_subset_options(
                            remaining_after_first,
                            target_span=sheet_width,
                            min_items=2 if len(remaining_after_first) >= 3 else 1,
                            prefer_large_depth=True,
                            limit=12,
                        )
                        if raw_second_row_candidates:
                            min_second_span = sheet_width * (0.72 if len(remaining_after_first) >= 4 else 0.58)
                            second_row_candidates = [
                                choice for choice in raw_second_row_candidates if _band_span(choice) >= min_second_span
                            ] or raw_second_row_candidates
                        else:
                            second_row_candidates = [[]]
                    for second_row in second_row_candidates:
                        second_ids = {str(part["id"]) for part, _ in second_row} if second_row else set()
                        remaining_after_second = [
                            part for part in remaining_after_first if str(part["id"]) not in second_ids
                        ]
                        third_row: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
                        if remaining_after_second:
                            third_variants = self._row_choice_variants(remaining_after_second, target_span=sheet_width)
                            if not third_variants:
                                continue
                            third_row = min(
                                third_variants,
                                key=lambda choice: (
                                    max(float(cand["width"]) for _, cand in choice),
                                    sheet_width - (
                                        sum(float(cand["height"]) for _, cand in choice)
                                        + self.kerf * max(0, len(choice) - 1)
                                    ),
                                    -sum(float(cand["height"]) for _, cand in choice),
                                ),
                            )
                        band_choices = [first_row] + ([second_row] if second_row else []) + ([third_row] if third_row else [])
                        band_depths = [max(float(cand["width"]) for _, cand in band) for band in band_choices if band]
                        if any(later > earlier + EPS for earlier, later in zip(band_depths, band_depths[1:])):
                            continue
                        placements = []
                        band_plan: List[Dict[str, Any]] = []
                        current_x = 0.0 if stack_from_left else sheet_length
                        remaining_x0 = 0.0
                        remaining_x1 = float(sheet_length)
                        valid = True
                        for band in band_choices:
                            band_depth = max(float(cand["width"]) for _, cand in band)
                            total_span = sum(float(cand["height"]) for _, cand in band) + self.kerf * max(0, len(band) - 1)
                            if total_span > sheet_width + EPS:
                                valid = False
                                break
                            current_y = 0.0 if span_from_bottom else (sheet_width - total_span)
                            band_y0 = current_y
                            band_anchor = "bl" if span_from_bottom else "tl"
                            band_rects: List[Rect] = []
                            for part, cand in band:
                                width = float(cand["width"])
                                height = float(cand["height"])
                                x = current_x if stack_from_left else (current_x - width)
                                y = current_y
                                rect = Rect(
                                    x=x,
                                    y=y,
                                    width=width,
                                    height=height,
                                    part_id=str(part["id"]),
                                    part_name=str(part["name"]),
                                    rotated=bool(cand.get("rotated", False)),
                                )
                                if not self._can_place(rect, placements + band_rects, sheet_length, sheet_width):
                                    valid = False
                                    break
                                band_rects.append(rect)
                                current_y += height + self.kerf
                            if not valid:
                                break
                            band_x0 = current_x if stack_from_left else (current_x - band_depth)
                            band_x1 = band_x0 + band_depth
                            band_plan.append(
                                {
                                    "band_x0": float(band_x0),
                                    "band_depth": float(band_depth),
                                    "band_y0": float(band_y0),
                                    "band_anchor": band_anchor,
                                    "stack_from_left": bool(stack_from_left),
                                    "span_from_bottom": bool(span_from_bottom),
                                    "part_ids": [str(part.get("id") or "") for part, _ in band],
                                    "part_names": [str(part.get("name") or "Part") for part, _ in band],
                                    "rects": [
                                        {
                                            "part_id": str(rect.part_id),
                                            "part_name": str(rect.part_name),
                                            "x": float(rect.x),
                                            "y": float(rect.y),
                                            "width": float(rect.width),
                                            "height": float(rect.height),
                                            "rotated": bool(rect.rotated),
                                        }
                                        for rect in band_rects
                                    ],
                                }
                            )
                            placements.extend(band_rects)
                            current_x = (current_x + band_depth + self.kerf) if stack_from_left else (current_x - band_depth - self.kerf)
                            if stack_from_left:
                                remaining_x0 = current_x
                            else:
                                remaining_x1 = current_x
                        if not valid:
                            continue
                        if len(placements) != len(parts):
                            continue
                        leftovers = _compute_leftovers_from_placements(
                            [
                                {
                                    "part_id": rect.part_id,
                                    "part_name": rect.part_name,
                                    "x": rect.x,
                                    "y": rect.y,
                                    "width": rect.width,
                                    "height": rect.height,
                                    "rotated": rect.rotated,
                                }
                                for rect in placements
                            ],
                            sheet_length=float(sheet_length),
                            sheet_width=float(sheet_width),
                        )
                        free_rects = [
                            FreeRect(
                                x=float(item["x"]),
                                y=float(item["y"]),
                                width=float(item["width"]),
                                height=float(item["height"]),
                            )
                            for item in leftovers
                            if float(item.get("width") or 0.0) > EPS and float(item.get("height") or 0.0) > EPS
                        ]
                        variants.append(
                            SheetResult(
                                id=sheet_id,
                                placements=placements,
                                free_rects=free_rects,
                                sheet_length=sheet_length,
                                sheet_width=sheet_width,
                                debug={
                                    "cut_plan_source": "single-sheet-strip",
                                    "cleanup_strategy": "single-sheet-strip",
                                    "stack_from_left": stack_from_left,
                                    "span_from_bottom": span_from_bottom,
                                    "band_count": len(band_choices),
                                    "band_plan": band_plan,
                                },
                            )
                        )
        if not variants:
            return None
        return min(
            variants,
            key=lambda item: self._single_sheet_offcut_score(
                item.placements,
                item.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            ),
        )

    def _run_guillotine_cleanup_variant(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
        sort_mode: str,
    ) -> Optional[SheetResult]:
        if not parts:
            return None
        from grainline._internal.optimization import GuillotineFFDAlgorithm, _compute_leftovers_from_placements

        prepared = [
            {
                "id": str(part["id"]),
                "name": str(part["name"]),
                "width": float(part.get("max_side") or 0.0),
                "height": float(part.get("min_side") or 0.0),
                "candidates": part.get("candidates"),
            }
            for part in parts
        ]
        guillotine = GuillotineFFDAlgorithm(
            kerf_mm=self.kerf,
            allow_rotation=self.allow_rotation,
            sort_mode=sort_mode,
        )
        placements_raw, unplaced, free_rects_raw, placement_trace = guillotine.optimize_sheet(prepared, sheet_length, sheet_width)
        if unplaced:
            return None
        placements = [
            Rect(
                x=float(rect.x),
                y=float(rect.y),
                width=float(rect.width),
                height=float(rect.height),
                part_id=str(rect.part_id),
                part_name=str(rect.part_name),
                rotated=bool(rect.rotated),
            )
            for rect in placements_raw
        ]
        free_rects = [
            FreeRect(
                x=float(rect.x),
                y=float(rect.y),
                width=float(rect.width),
                height=float(rect.height),
            )
            for rect in free_rects_raw
            if float(rect.width) > EPS and float(rect.height) > EPS
        ]
        return SheetResult(
            id=sheet_id,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            debug={
                "cut_plan_source": f"guillotine-{sort_mode}",
                "cleanup_strategy": f"guillotine-{sort_mode}",
                "placement_trace": [dict(item) for item in placement_trace],
            },
        )

    def _run_partition_first_baseline_variant(
        self,
        parts: Sequence[Dict[str, Any]],
        *,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
    ) -> Optional[SheetResult]:
        if not parts:
            return None
        from grainline._internal.partition_first import PartitionFirstAlgorithm
        from grainline._internal.optimization import _compute_leftovers_from_placements

        baseline_parts = [
            SimpleNamespace(
                id=str(part["id"]),
                name=str(part["name"]),
                quantity=1,
                length_mm=float(part.get("max_side") or 0.0),
                width_mm=float(part.get("min_side") or 0.0),
                candidates=part.get("candidates"),
            )
            for part in parts
        ]
        baseline = PartitionFirstAlgorithm(kerf_mm=self.kerf, allow_rotation=self.allow_rotation)
        result = baseline.optimize(
            parts=baseline_parts,
            boards=[],
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            max_sheets=1,
        )
        sheets = result.get("sheets") or []
        if not sheets:
            return None
        sheet = sheets[0]
        placements_raw = sheet.get("placements") or []
        if len(placements_raw) != len(parts):
            return None
        placements = [
            Rect(
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item["width"]),
                height=float(item["height"]),
                part_id=str(item["part_id"]),
                part_name=str(item["part_name"]),
                rotated=bool(item.get("rotated", False)),
            )
            for item in placements_raw
        ]
        variants: List[Tuple[str, List[Rect]]] = [("partition-first-baseline", placements)]
        compacted_to_top = self._compact_placements_along_x(
            placements,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            toward_min=True,
        )
        if any(abs(a.x - b.x) > EPS for a, b in zip(placements, compacted_to_top)):
            variants.append(("partition-first-baseline-compact-top", compacted_to_top))

        best_sheet: Optional[SheetResult] = None
        best_score: Optional[Tuple[float, ...]] = None
        for label, candidate_placements in variants:
            leftovers = _compute_leftovers_from_placements(
                [
                    {
                        "part_id": rect.part_id,
                        "part_name": rect.part_name,
                        "x": rect.x,
                        "y": rect.y,
                        "width": rect.width,
                        "height": rect.height,
                        "rotated": rect.rotated,
                    }
                    for rect in candidate_placements
                ],
                sheet_length=float(sheet_length),
                sheet_width=float(sheet_width),
            )
            free_rects = [
                FreeRect(
                    x=float(item["x"]),
                    y=float(item["y"]),
                    width=float(item["width"]),
                    height=float(item["height"]),
                )
                for item in leftovers
                if float(item.get("width") or 0.0) > EPS and float(item.get("height") or 0.0) > EPS
            ]
            score = self._single_sheet_offcut_score(
                candidate_placements,
                free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_sheet = SheetResult(
                    id=sheet_id,
                    placements=candidate_placements,
                    free_rects=free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                    debug={"cleanup_strategy": label},
                )
        return best_sheet

    def _cleanup_target_indices(self, sheets: Sequence[SheetResult]) -> List[int]:
        if not sheets:
            return []
        if len(sheets) == 1:
            sheet = sheets[0]
            waste_ratio = max(0.0, 1.0 - (sheet.used_area / max(sheet.total_area, 1.0)))
            free_metrics = self._timed_free_rect_metrics(
                sheet.free_rects,
                sheet_length=sheet.sheet_length,
                sheet_width=sheet.sheet_width,
            )
            components = self._timed_component_count(sheet.placements)
            if waste_ratio < 0.3:
                return []
            if components <= 1 and free_metrics["main_ratio"] >= 0.5:
                return []
            return [0]
        ranked: List[Tuple[float, float, int]] = []
        for index, sheet in enumerate(sheets):
            waste_ratio = max(0.0, 1.0 - (sheet.used_area / max(sheet.total_area, 1.0)))
            ranked.append((waste_ratio, -sheet.utilization, index))
        ranked.sort(reverse=True)
        target = ranked[0][2]
        if ranked[0][0] < 0.15:
            return []
        return [target]

    def _optimize_sheet(
        self,
        remaining: Sequence[Dict[str, Any]],
        *,
        board_id: str,
        target: int,
        board_structure_profile: Dict[str, Any] | None,
        sheet_length: float,
        sheet_width: float,
        sheet_id: int,
        max_sheets: int,
        must_place_all: bool = False,
    ) -> SheetResult:
        best: Optional[SheetResult] = None
        best_score: Optional[Tuple[float, ...]] = None
        best_label = "auto"
        seed_results: list[dict[str, Any]] = []
        seen_seed_signatures: dict[tuple[Any, ...], dict[str, Any]] = {}
        best_signature_key: tuple[Any, ...] | None = None
        duplicate_seed_hits = 0
        early_stop_triggered = False
        early_stop_reason: str | None = None
        min_seed_trials = 4
        seed_options = self._seed_options(
            remaining,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        seed_trials_total = len(seed_options)
        sheet_started = time.perf_counter()
        self._profile_log(
            "sheet_start",
            sheet_id=sheet_id,
            parts=len(remaining),
            seed_count=len(seed_options),
        )
        for seed_index, (label, seed_part_id, seed_candidate) in enumerate(seed_options, start=1):
            if best is not None and self.deadline is not None and time.perf_counter() >= self.deadline:
                early_stop_triggered = True
                early_stop_reason = "time_limit"
                break
            seed_started = time.perf_counter()
            result = self._run_seeded_layout(
                remaining,
                board_id=board_id,
                target=target,
                sheet_id=sheet_id,
                board_structure_profile=board_structure_profile,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                seed_part_id=seed_part_id,
                seed_candidate=seed_candidate,
                future_sheet_slots=max(0, int(max_sheets) - int(sheet_id)),
            )
            seed_dt_ms = (time.perf_counter() - seed_started) * 1000.0
            score = self._sheet_progress_tuple(
                result.placements,
                result.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            guillotine = self._timed_guillotine_metrics(
                result.placements,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            free_metrics = self._timed_free_rect_metrics(
                result.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            slicing = self._fast_slicing_proxy(
                result.placements,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            void_metrics = self._timed_enclosed_void_metrics(
                result.placements,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            score = (
                float(void_metrics["enclosed_void_count"]),
                float(void_metrics["enclosed_void_area"]),
                0.0 if bool(guillotine["is_guillotine"]) else 1.0,
                float(guillotine["guillotine_unreleased_count"]),
                *score,
            )
            if must_place_all:
                # Planned sheet: a seed that leaves assigned parts off the sheet cannot win.
                score = (float(len(remaining) - len(result.placements)), *score)
            seed_signature_key, seed_signature_debug = self._seed_signature(
                result,
                input_part_count=len(remaining),
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                free_metrics=free_metrics,
                guillotine=guillotine,
                slicing=slicing,
            )
            prior_signature = seen_seed_signatures.get(seed_signature_key)
            is_duplicate = prior_signature is not None
            duplicate_against = str(prior_signature.get("seed")) if isinstance(prior_signature, dict) else None
            best_before_seed = best_score
            duplicate_without_improving_best = (
                bool(is_duplicate)
                and best_before_seed is not None
                and best_signature_key is not None
                and seed_signature_key == best_signature_key
                and score >= best_before_seed
            )
            self._profile_log(
                "sheet_seed",
                sheet_id=sheet_id,
                seed=label,
                dt_ms=seed_dt_ms,
                placements=len(result.placements),
                enclosed_voids=void_metrics["enclosed_void_count"],
                guillotine_unreleased=guillotine["guillotine_unreleased_count"],
                duplicate=is_duplicate,
            )
            seed_results.append(
                {
                    "seed": label,
                    "dt_ms": round(seed_dt_ms, 1),
                    "placements": len(result.placements),
                    "enclosed_voids": float(void_metrics["enclosed_void_count"]),
                    "guillotine_unreleased": float(guillotine["guillotine_unreleased_count"]),
                    "is_guillotine": bool(guillotine["is_guillotine"]),
                    "seed_signature_key": "|".join(str(item) for item in seed_signature_key),
                    "seed_signature_debug": seed_signature_debug,
                    "seed_duplicate": bool(is_duplicate),
                    "seed_duplicate_against": duplicate_against,
                }
            )
            if best_score is None or score < best_score:
                best = result
                best_score = score
                best_label = label
                best_signature_key = seed_signature_key
            if prior_signature is None or score < cast(tuple[Any, ...], prior_signature.get("score")):
                seen_seed_signatures[seed_signature_key] = {
                    "seed": str(label),
                    "score": score,
                }
            if duplicate_without_improving_best:
                duplicate_seed_hits += 1
            if (
                self.seed_early_stop and seed_index >= min_seed_trials
                and best_score is not None
                and duplicate_seed_hits >= 2
                and duplicate_without_improving_best
            ):
                early_stop_triggered = True
                early_stop_reason = "duplicate_seed_hits"
                self._profile_log(
                    "seed_early_stop",
                    sheet_id=sheet_id,
                    seed=label,
                    executed=seed_index,
                    total=seed_trials_total,
                    duplicate_seed_hits=duplicate_seed_hits,
                )
                break
        for entry in seed_results:
            entry["won"] = str(entry["seed"]) == best_label
            entry["seed_early_stop_triggered"] = bool(early_stop_triggered)
            entry["seed_early_stop_reason"] = early_stop_reason
            entry["seed_trials_total"] = int(seed_trials_total)
            entry["seed_trials_executed"] = int(len(seed_results))
            entry["seed_trials_skipped"] = int(max(0, seed_trials_total - len(seed_results)))
            entry["duplicate_seed_hits"] = int(duplicate_seed_hits)

        if best is None:
            best = SheetResult(
                id=sheet_id,
                placements=[],
                free_rects=[FreeRect(0.0, 0.0, sheet_length, sheet_width)],
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
        best.id = sheet_id
        sheet_dt_ms = (time.perf_counter() - sheet_started) * 1000.0
        self._profile_add("optimize_sheet", sheet_dt_ms)
        self._profile_log(
            "sheet_done",
            sheet_id=sheet_id,
            dt_ms=sheet_dt_ms,
            best_seed=best_label,
            placements=len(best.placements),
        )
        best_seed_entry = next((entry for entry in seed_results if str(entry["seed"]) == best_label), None)
        self._profile_log(
            "seed_summary",
            sheet_id=sheet_id,
            best_seed=best_label,
            seed_count=len(seed_results),
            seed_trials_total=seed_trials_total,
            seed_trials_skipped=max(0, seed_trials_total - len(seed_results)),
            duplicate_seed_hits=duplicate_seed_hits,
            early_stop=early_stop_triggered,
            best_dt_ms=(best_seed_entry or {}).get("dt_ms"),
            best_placements=(best_seed_entry or {}).get("placements"),
            best_is_guillotine=(best_seed_entry or {}).get("is_guillotine"),
            best_enclosed_voids=(best_seed_entry or {}).get("enclosed_voids"),
            best_guillotine_unreleased=(best_seed_entry or {}).get("guillotine_unreleased"),
        )
        best.debug["seed_summaries"] = seed_results
        best.debug["best_seed"] = best_label
        best.debug["seed_trials_total"] = int(seed_trials_total)
        best.debug["seed_trials_executed"] = int(len(seed_results))
        best.debug["seed_trials_skipped"] = int(max(0, seed_trials_total - len(seed_results)))
        best.debug["duplicate_seed_hits"] = int(duplicate_seed_hits)
        best.debug["seed_early_stop_triggered"] = bool(early_stop_triggered)
        best.debug["seed_early_stop_reason"] = early_stop_reason
        return best

    def _sheet_from_plan(
        self,
        planned: PlannedSheet,
        assigned: Sequence[Dict[str, Any]],
        *,
        sheet_id: int,
        sheet_length: float,
        sheet_width: float,
    ) -> SheetResult:
        names = {str(item["id"]): str(item["name"]) for item in assigned}
        placements = [
            Rect(
                x=float(item.x),
                y=float(item.y),
                width=float(item.width),
                height=float(item.height),
                part_id=str(item.part_id),
                part_name=names.get(str(item.part_id), "Part"),
                rotated=bool(item.rotated),
            )
            for item in planned.placements
        ]
        free_rects = [FreeRect(x, y, width, height) for x, y, width, height in planned.free_rects]
        return SheetResult(
            id=sheet_id,
            placements=placements,
            free_rects=free_rects,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            debug={
                "cut_plan_source": "planned-sheet",
                "best_seed": "planned-sheet",
                # Only a sheet split the engine's way can be replayed by the isolation cut recorder.
                "steps": [
                    {
                        "action": "place",
                        "anchor": "bl",
                        "host_free_rect": {"x": item.host[0], "y": item.host[1], "width": item.host[2], "height": item.host[3]},
                    }
                    for item in planned.placements
                ]
                if planned.engine_split
                else [],
            },
        )

    def optimize_with_sheet_plan(
        self,
        parts: List[Any],
        boards: List[Any],
        sheet_length: float,
        sheet_width: float,
        *,
        sheet_count: int,
        time_budget_s: float | None = None,
        max_plans: int = SHEET_PLAN_MAX_PLANS,
        layout_budget_s: float | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Try to reach `sheet_count` sheets by fixing the split first and laying out each sheet after.

        Returns a normal optimize() result with nothing unplaced, or None when no split was found
        in the budget or the engine could not lay one out. `layout_budget_s` caps the time spent
        laying out candidate splits; a split already being laid out is allowed to finish.
        """
        started = time.perf_counter()
        self.last_sheet_plan_stats = {"plans_found": 0, "plans_laid_out": 0}
        expanded = self._expand_parts(parts, sheet_length, sheet_width)
        pieces = [(str(item["id"]), self._get_candidates(item), float(item["max_area"])) for item in expanded]
        plans = find_sheet_plans(
            pieces,
            sheet_count=int(sheet_count),
            sheet_length=float(sheet_length),
            sheet_width=float(sheet_width),
            kerf=float(self.kerf),
            time_budget_s=time_budget_s,
            max_tries=self.partition_max_tries,
            seed=self.random_seed,
            stats=self.last_sheet_plan_stats,
            max_plans=max_plans,
        )
        self.last_sheet_plan_stats["plans_found"] = len(plans)
        for plan in plans:
            if layout_budget_s is not None and (time.perf_counter() - started) > float(layout_budget_s):
                break
            self.last_sheet_plan_stats["plans_laid_out"] += 1
            result = self.optimize(
                parts=parts,
                boards=boards,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                max_sheets=len(plan),
                sheet_plan=plan,
            )
            if int(result.get("unplaced_count") or 0) != 0 or int(result.get("sheets_used") or 0) > int(sheet_count):
                continue
            # Check the replay, not merely nonempty steps. Exact stock-sized parts
            # legitimately require zero cuts.
            if any(not ((sheet.get("cut_plan_meta") or {}).get("validation") or {}).get("passed")
                   for sheet in (result.get("sheets") or [])):
                continue
            return result
        return None

    def optimize(
        self,
        parts: List[Any],
        boards: List[Any],
        sheet_length: float,
        sheet_width: float,
        max_sheets: int | None = None,
        sheet_plan: Sequence[PlannedSheet] | None = None,
    ) -> Dict[str, Any]:
        max_sheets = int(max_sheets or 1)
        self._profile_reset()
        expanded_parts = self._expand_parts(parts, sheet_length, sheet_width)
        board_structure_profile = self._board_structure_profile(
            expanded_parts,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
        )
        remaining = list(expanded_parts)
        all_sheets: List[SheetResult] = []
        placed_ids: set[str] = set()
        target_dead_end_info: Dict[str, Any] | None = None
        seed_summaries: list[dict[str, Any]] = []
        cleanup_decisions: list[dict[str, Any]] = []
        self._profile_log(
            "run_start",
            expanded_parts=len(expanded_parts),
            max_sheets=max_sheets,
            seed_limit=self.seed_limit,
            candidate_pool_limit=self.candidate_pool_limit,
        )

        for sheet_id in range(1, max_sheets + 1):
            if not remaining:
                break
            planned = sheet_plan[sheet_id - 1] if sheet_plan is not None and sheet_id <= len(sheet_plan) else None
            if sheet_plan is not None and planned is None:
                break
            if planned is not None:
                # The split across sheets is fixed; the engine only decides how this sheet is laid out.
                assigned = [item for item in remaining if str(item["id"]) in planned.part_ids]
                sheet = self._optimize_sheet(
                    assigned,
                    board_id=str(getattr(boards[0], "id", "") or "board"),
                    target=max_sheets,
                    board_structure_profile=board_structure_profile,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                    sheet_id=sheet_id,
                    max_sheets=sheet_id,
                    must_place_all=True,
                )
                if len(sheet.placements) != len(assigned):
                    sheet = self._sheet_from_plan(
                        planned,
                        assigned,
                        sheet_id=sheet_id,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    self._profile_log("planned_sheet_fallback", sheet_id=sheet_id, parts=len(assigned))
            else:
                sheet = self._optimize_sheet(
                    remaining,
                    board_id=str(getattr(boards[0], "id", "") or "board"),
                    target=max_sheets,
                    board_structure_profile=board_structure_profile,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                    sheet_id=sheet_id,
                    max_sheets=max_sheets,
                )
            if not sheet.placements:
                break
            all_sheets.append(sheet)
            seed_summaries.append(
                {
                    "sheet_id": int(sheet_id),
                    "best_seed": str(sheet.debug.get("best_seed") or "auto"),
                    "seed_count": len(list(sheet.debug.get("seed_summaries") or [])),
                    "seed_trials_total": int(sheet.debug.get("seed_trials_total") or 0),
                    "seed_trials_executed": int(sheet.debug.get("seed_trials_executed") or 0),
                    "seed_trials_skipped": int(sheet.debug.get("seed_trials_skipped") or 0),
                    "duplicate_seed_hits": int(sheet.debug.get("duplicate_seed_hits") or 0),
                    "seed_early_stop_triggered": bool(sheet.debug.get("seed_early_stop_triggered")),
                    "seed_early_stop_reason": sheet.debug.get("seed_early_stop_reason"),
                    "candidates": list(sheet.debug.get("seed_summaries") or []),
                }
            )
            current_ids = {rect.part_id for rect in sheet.placements}
            placed_ids.update(current_ids)
            remaining = [item for item in remaining if str(item["id"]) not in current_ids]
            if sheet_plan is not None:
                continue
            dead_end = self._target_dead_end_check(
                remaining,
                free_rects=sheet.free_rects,
                future_sheet_slots=max(0, max_sheets - sheet_id),
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            if dead_end is not None:
                target_dead_end_info = {
                    "target": int(max_sheets),
                    "sheet_id": int(sheet_id),
                    **dead_end,
                }
                self._profile_log(
                    "target_dead_end",
                    target=max_sheets,
                    sheet_id=sheet_id,
                    **dead_end,
                )
                break

        if all_sheets and target_dead_end_info is None:
            part_by_id = {str(item["id"]): item for item in expanded_parts}
            cleanup_targets = set(self._cleanup_target_indices(all_sheets))

            def run_cleanup_variant(name: str, fn: Any) -> tuple[Optional[SheetResult], float]:
                started = time.perf_counter()
                result = fn()
                dt_ms = (time.perf_counter() - started) * 1000.0
                self._profile_add(name, dt_ms)
                return result, dt_ms

            for index, sheet in enumerate(all_sheets):
                current_parts = [part_by_id.get(rect.part_id) for rect in sheet.placements]
                sheet_parts = [part for part in current_parts if part is not None]
                if len(sheet_parts) != len(sheet.placements):
                    continue
                if index not in cleanup_targets:
                    continue
                improved, improved_dt_ms = run_cleanup_variant(
                    "cleanup_sheet",
                    lambda: self._cleanup_sheet(
                        sheet_parts,
                        board_id=str(getattr(boards[0], "id", "") or "board"),
                        target=max_sheets,
                        board_structure_profile=board_structure_profile,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                        sheet_id=sheet.id,
                    ),
                )
                if improved is None or len(improved.placements) != len(sheet.placements):
                    self._profile_log(
                        "cleanup_branch",
                        sheet_id=sheet.id,
                        branch="cleanup_sheet",
                        dt_ms=improved_dt_ms,
                        valid=False,
                    )
                    continue
                current_components = self._timed_component_count(sheet.placements)
                improved_components = self._timed_component_count(improved.placements)
                self._profile_log(
                    "cleanup_branch",
                    sheet_id=sheet.id,
                    branch="cleanup_sheet",
                    dt_ms=improved_dt_ms,
                    valid=True,
                    placements=len(improved.placements),
                    components=improved_components,
                )
                if improved_components > current_components:
                    continue
                current_score = self._cleanup_score(
                    sheet.placements,
                    sheet.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                improved_cleanup, improved_cleanup_dt_ms = run_cleanup_variant(
                    "cleanup_layout",
                    lambda: self._run_cleanup_layout(
                        sheet_parts,
                        sheet_id=sheet.id,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                        seed_part_id=None,
                        seed_candidate=None,
                    ),
                )
                shelf_cleanup = None
                strip_cleanup, strip_cleanup_dt_ms = (
                    run_cleanup_variant(
                        "cleanup_strip",
                        lambda: self._run_single_sheet_strip_cleanup(
                            sheet_parts,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                            sheet_id=sheet.id,
                        ),
                    )
                    if len(all_sheets) == 1
                    else (None, 0.0)
                )
                partition_cleanup, partition_cleanup_dt_ms = (
                    run_cleanup_variant(
                        "cleanup_partition",
                        lambda: self._run_partition_first_baseline_variant(
                            sheet_parts,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                            sheet_id=sheet.id,
                        ),
                    )
                    if len(all_sheets) == 1
                    else (None, 0.0)
                )
                guillotine_variants: List[SheetResult] = []
                guillotine_variant_logs: List[Tuple[str, float, Optional[SheetResult]]] = []
                if len(all_sheets) == 1 or sheet.utilization <= 55.0:
                    for sort_mode in ("area", "max_side", "short_side", "perimeter"):
                        candidate, dt_ms = run_cleanup_variant(
                            f"cleanup_guillotine_{sort_mode}",
                            lambda sort_mode=sort_mode: self._run_guillotine_cleanup_variant(
                                sheet_parts,
                                sheet_length=sheet_length,
                                sheet_width=sheet_width,
                                sheet_id=sheet.id,
                                sort_mode=sort_mode,
                            ),
                        )
                        guillotine_variant_logs.append((sort_mode, dt_ms, candidate))
                        if candidate is not None:
                            guillotine_variants.append(candidate)
                self._profile_log(
                    "cleanup_branch",
                    sheet_id=sheet.id,
                    branch="cleanup_layout",
                    dt_ms=improved_cleanup_dt_ms,
                    valid=len(improved_cleanup.placements) == len(sheet.placements),
                    placements=len(improved_cleanup.placements),
                )
                if len(all_sheets) == 1:
                    self._profile_log(
                        "cleanup_branch",
                        sheet_id=sheet.id,
                        branch="single_sheet_strip",
                        dt_ms=strip_cleanup_dt_ms,
                        valid=strip_cleanup is not None and len(strip_cleanup.placements) == len(sheet.placements),
                        placements=len(strip_cleanup.placements) if strip_cleanup is not None else 0,
                    )
                    self._profile_log(
                        "cleanup_branch",
                        sheet_id=sheet.id,
                        branch="partition_first_baseline",
                        dt_ms=partition_cleanup_dt_ms,
                        valid=partition_cleanup is not None and len(partition_cleanup.placements) == len(sheet.placements),
                        placements=len(partition_cleanup.placements) if partition_cleanup is not None else 0,
                    )
                for sort_mode, dt_ms, candidate in guillotine_variant_logs:
                    self._profile_log(
                        "cleanup_branch",
                        sheet_id=sheet.id,
                        branch=f"guillotine_{sort_mode}",
                        dt_ms=dt_ms,
                        valid=candidate is not None and len(candidate.placements) == len(sheet.placements),
                        placements=len(candidate.placements) if candidate is not None else 0,
                    )
                improved_score = self._cleanup_score(
                    improved.placements,
                    improved.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                alt_score = self._cleanup_score(
                    improved_cleanup.placements,
                    improved_cleanup.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                ) if len(improved_cleanup.placements) == len(sheet.placements) else None
                shelf_score = self._cleanup_score(
                    shelf_cleanup.placements,
                    shelf_cleanup.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                ) if shelf_cleanup is not None and len(shelf_cleanup.placements) == len(sheet.placements) else None
                strip_score = self._cleanup_score(
                    strip_cleanup.placements,
                    strip_cleanup.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                ) if strip_cleanup is not None and len(strip_cleanup.placements) == len(sheet.placements) else None
                alt_components = (
                    self._timed_component_count(improved_cleanup.placements)
                    if alt_score is not None
                    else None
                )
                shelf_components = (
                    self._timed_component_count(shelf_cleanup.placements)
                    if shelf_score is not None and shelf_cleanup is not None
                    else None
                )
                strip_components = (
                    self._timed_component_count(strip_cleanup.placements)
                    if strip_score is not None and strip_cleanup is not None
                    else None
                )
                partition_score = self._cleanup_score(
                    partition_cleanup.placements,
                    partition_cleanup.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                ) if partition_cleanup is not None and len(partition_cleanup.placements) == len(sheet.placements) else None
                partition_components = (
                    self._timed_component_count(partition_cleanup.placements)
                    if partition_score is not None and partition_cleanup is not None
                    else None
                )
                chosen_sheet = improved
                chosen_score = improved_score
                if alt_score is not None and alt_components is not None and alt_components <= current_components and alt_score < chosen_score:
                    chosen_sheet = improved_cleanup
                    chosen_score = alt_score
                if shelf_score is not None and shelf_components is not None and shelf_components <= current_components and shelf_score < chosen_score:
                    chosen_sheet = shelf_cleanup
                    chosen_score = shelf_score
                if strip_score is not None and strip_components is not None and strip_components <= current_components and strip_score < chosen_score:
                    chosen_sheet = strip_cleanup
                    chosen_score = strip_score
                if partition_score is not None and partition_components is not None and partition_components <= current_components and partition_score < chosen_score:
                    chosen_sheet = partition_cleanup
                    chosen_score = partition_score
                low_util_target = len(all_sheets) == 1 or sheet.utilization <= 55.0
                chosen_strategy = str(chosen_sheet.debug.get("cleanup_strategy") or "cleanup-seeded")
                if low_util_target:
                    score_fn = (
                        self._single_sheet_offcut_score
                        if len(all_sheets) == 1
                        else self._offcut_shaping_score
                    )
                    current_offcut_score = score_fn(
                        sheet.placements,
                        sheet.free_rects,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    best_offcut_sheet = sheet
                    best_offcut_score = current_offcut_score
                    for candidate in [improved, improved_cleanup, shelf_cleanup, strip_cleanup, partition_cleanup, *guillotine_variants]:
                        if candidate is None or len(candidate.placements) != len(sheet.placements):
                            continue
                        if self._timed_component_count(candidate.placements) > current_components:
                            continue
                        candidate_offcut_score = score_fn(
                            candidate.placements,
                            candidate.free_rects,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        )
                        if candidate_offcut_score < best_offcut_score:
                            best_offcut_sheet = candidate
                            best_offcut_score = candidate_offcut_score
                    self._profile_log(
                        "cleanup_decision",
                        sheet_id=sheet.id,
                        mode="offcut",
                        current_components=current_components,
                        chosen_strategy=str(best_offcut_sheet.debug.get("cleanup_strategy") or "original"),
                        chosen_is_original=(best_offcut_sheet is sheet),
                    )
                    cleanup_decisions.append(
                        {
                            "sheet_id": int(sheet.id),
                            "mode": "offcut",
                            "chosen_strategy": str(best_offcut_sheet.debug.get("cleanup_strategy") or "original"),
                            "chosen_is_original": bool(best_offcut_sheet is sheet),
                            "candidate_count": int(
                                sum(
                                    1
                                    for candidate in [improved, improved_cleanup, shelf_cleanup, strip_cleanup, partition_cleanup, *guillotine_variants]
                                    if candidate is not None and len(candidate.placements) == len(sheet.placements)
                                )
                            ),
                            "improved_any": bool(best_offcut_sheet is not sheet),
                        }
                    )
                    if best_offcut_sheet is not sheet:
                        best_offcut_sheet.id = sheet.id
                        all_sheets[index] = best_offcut_sheet
                    continue
                current_free_metrics = self._timed_free_rect_metrics(
                    sheet.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                chosen_components = self._timed_component_count(chosen_sheet.placements)
                chosen_free_metrics = self._timed_free_rect_metrics(
                    chosen_sheet.free_rects,
                    sheet_length=sheet_length,
                    sheet_width=sheet_width,
                )
                if len(all_sheets) == 1:
                    single_sheet_improved = (
                        chosen_components < current_components
                        or (
                            chosen_components == current_components
                            and chosen_free_metrics["largest_area"] >= current_free_metrics["largest_area"] * 1.4
                            and chosen_free_metrics["reusable_area"] >= current_free_metrics["reusable_area"] * 1.4
                            and chosen_free_metrics["bottom_right_area"] >= current_free_metrics["bottom_right_area"]
                        )
                    )
                    self._profile_log(
                        "cleanup_decision",
                        sheet_id=sheet.id,
                        mode="single_sheet",
                        current_components=current_components,
                        chosen_strategy=chosen_strategy,
                        chosen_is_original=(chosen_sheet is sheet),
                        chosen_score_head=chosen_score[0] if chosen_score else None,
                    )
                    cleanup_decisions.append(
                        {
                            "sheet_id": int(sheet.id),
                            "mode": "single_sheet",
                            "chosen_strategy": chosen_strategy,
                            "chosen_is_original": bool(chosen_sheet is sheet),
                            "candidate_count": int(
                                sum(
                                    1
                                    for candidate in [improved, improved_cleanup, shelf_cleanup, strip_cleanup, partition_cleanup, *guillotine_variants]
                                    if candidate is not None and len(candidate.placements) == len(sheet.placements)
                                )
                            ),
                            "improved_any": bool(chosen_score < current_score and single_sheet_improved),
                        }
                    )
                    if chosen_score < current_score and single_sheet_improved:
                        chosen_sheet.id = sheet.id
                        all_sheets[index] = chosen_sheet
                    continue
                self._profile_log(
                    "cleanup_decision",
                    sheet_id=sheet.id,
                    mode="multi_sheet",
                    current_components=current_components,
                    chosen_strategy=chosen_strategy,
                    chosen_is_original=(chosen_sheet is sheet),
                    chosen_score_head=chosen_score[0] if chosen_score else None,
                )
                cleanup_decisions.append(
                    {
                        "sheet_id": int(sheet.id),
                        "mode": "multi_sheet",
                        "chosen_strategy": chosen_strategy,
                        "chosen_is_original": bool(chosen_sheet is sheet),
                        "candidate_count": int(
                            sum(
                                1
                                for candidate in [improved, improved_cleanup, shelf_cleanup, strip_cleanup, partition_cleanup, *guillotine_variants]
                                if candidate is not None and len(candidate.placements) == len(sheet.placements)
                            )
                        ),
                        "improved_any": bool(chosen_score < current_score),
                    }
                )
                if chosen_score < current_score:
                    chosen_sheet.id = sheet.id
                    all_sheets[index] = chosen_sheet

        total_used_area = sum(sheet.used_area for sheet in all_sheets)
        total_area = sum(sheet.total_area for sheet in all_sheets)
        efficiency = (total_used_area / total_area * 100.0) if total_area > 0 else 0.0

        sheets_payload: List[Dict[str, Any]] = []
        for sheet in all_sheets:
            free_metrics = self._timed_free_rect_metrics(
                sheet.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            offcut_regions, offcut_objects, offcut_summary = self._build_offcut_regions(
                sheet.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                free_metrics=free_metrics,
            )
            equivalence_features = self._layout_equivalence_features(
                sheet.placements,
                sheet.free_rects,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
            )
            cut_plan_source = str(sheet.debug.get("cut_plan_source") or "")
            cut_plan_modes = _build_cut_plan_modes(
                base_steps=[],
                placements=sheet.placements,
                offcut_regions=offcut_regions,
                kerf_mm=self.kerf,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                sheet_id=sheet.id,
                source_strategy=cut_plan_source or "layout",
            )
            default_mode = cut_plan_modes.get("default") or {"steps": [], "part_cut_paths": {}, "meta": {}}
            if not (default_mode.get("steps") or []):
                cut_plan_steps: List[Dict[str, Any]] = []
                if cut_plan_source in {"seeded-layout", "cleanup-seeded", "cleanup-layout", "planned-sheet"}:
                    cut_plan_steps = _build_isolation_cut_plan_steps(
                        placements=sheet.placements,
                        debug_steps=[item for item in (sheet.debug.get("steps") or []) if isinstance(item, dict)],
                        kerf_mm=self.kerf,
                        sheet_id=sheet.id,
                        source_strategy=cut_plan_source,
                    )
                elif cut_plan_source == "single-sheet-strip":
                    cut_plan_steps = _build_single_sheet_strip_cut_plan_steps(
                        band_plan=[item for item in (sheet.debug.get("band_plan") or []) if isinstance(item, dict)],
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                        kerf_mm=self.kerf,
                        sheet_id=sheet.id,
                    )
                elif cut_plan_source.startswith("guillotine-"):
                    cut_plan_steps = _build_isolation_cut_plan_steps(
                        placements=sheet.placements,
                        debug_steps=[
                            {
                                "action": "place",
                                "host_free_rect": item.get("host_free_rect"),
                                "anchor": item.get("anchor"),
                            }
                            for item in (sheet.debug.get("placement_trace") or [])
                            if isinstance(item, dict)
                        ],
                        kerf_mm=self.kerf,
                        sheet_id=sheet.id,
                        source_strategy=cut_plan_source,
                    )
                if cut_plan_steps:
                    cut_plan_modes = _build_cut_plan_modes(
                        base_steps=cut_plan_steps,
                        placements=sheet.placements,
                        offcut_regions=offcut_regions,
                        kerf_mm=self.kerf,
                        sheet_length=None,
                        sheet_width=None,
                        sheet_id=None,
                        source_strategy=cut_plan_source or "layout",
                    )
            default_mode = cut_plan_modes.get("default") or {"steps": [], "part_cut_paths": {}, "meta": {}}
            protect_mode = cut_plan_modes.get("protect") or {"steps": [], "part_cut_paths": {}, "meta": {}}
            default_steps = [dict(step) for step in (default_mode.get("steps") or []) if isinstance(step, dict)]
            cut_plan_validation = validate_cut_plan(
                [_part_payload(rect) for rect in sheet.placements], default_steps,
                sheet_length=sheet_length, sheet_width=sheet_width, kerf_mm=self.kerf,
            )
            if not cut_plan_validation["passed"]:
                raise RuntimeError(f"Incomplete cut plan for sheet {sheet.id}: {cut_plan_validation['errors']}")
            part_cut_paths = default_mode.get("part_cut_paths") if isinstance(default_mode.get("part_cut_paths"), dict) else {}
            sheets_payload.append(
                {
                    "id": sheet.id,
                    "placements": [
                        {
                            "part_id": rect.part_id,
                            "part_name": rect.part_name,
                            "x": rect.x,
                            "y": rect.y,
                            "width": rect.width,
                            "height": rect.height,
                            "rotated": rect.rotated,
                        }
                        for rect in sheet.placements
                    ],
                    "utilization": round(sheet.utilization, 2),
                    "used_area": sheet.used_area,
                    "total_area": sheet.total_area,
                    "leftovers": [
                        {
                            "x": rect.x,
                            "y": rect.y,
                            "width": rect.width,
                            "height": rect.height,
                            "area": rect.area,
                        }
                        for rect in sheet.free_rects
                        if rect.width > EPS and rect.height > EPS and rect.area > EPS
                    ],
                    "offcut_profile": {
                        "largest_rect_area": round(free_metrics["largest_area"], 2),
                        "reusable_rect_count": int(
                            sum(
                                1
                                for rect in sheet.free_rects
                                if min(rect.width, rect.height) >= 180.0
                                and max(rect.width, rect.height) / max(min(rect.width, rect.height), 1.0) <= 4.5
                            )
                        ),
                        "skinny_rect_ratio": round(
                            (
                                free_metrics["skinny_area"] / free_metrics["total_area"]
                                if free_metrics["total_area"] > 0
                                else 0.0
                            ),
                            4,
                        ),
                    },
                    "offcut_regions": offcut_regions,
                    "offcut_objects": offcut_objects,
                    "offcut_summary": offcut_summary,
                    "workflow_metrics": {
                        **self._timed_guillotine_metrics(
                            sheet.placements,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        ),
                        **self._timed_void_cell_metrics(
                            sheet.placements,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        ),
                        **self._timed_junction_metrics(
                            sheet.placements,
                            sheet_length=sheet_length,
                            sheet_width=sheet_width,
                        ),
                    },
                    "cut_plan_modes": {
                        "default": {
                            "steps": default_steps,
                            "part_cut_paths": part_cut_paths,
                            "meta": dict(default_mode.get("meta") or {}),
                        },
                        "protect": {
                            "steps": [dict(step) for step in (protect_mode.get("steps") or []) if isinstance(step, dict)],
                            "part_cut_paths": dict(protect_mode.get("part_cut_paths") or {}),
                            "meta": dict(protect_mode.get("meta") or {}),
                        },
                    },
                    "default_cut_plan_mode": "default",
                    "cut_plan_steps": default_steps,
                    "cut_plan_meta": {
                        "version": "machine_v2",
                        "validation": cut_plan_validation,
                        "step_count": int(len(default_steps)),
                        "source_strategy": str((default_steps[0] or {}).get("source_strategy") or "")
                        if default_steps
                        else cut_plan_source,
                        "part_path_count": int(len(part_cut_paths)),
                        "modes": ["default", "protect"],
                        "protect_step_count": int(len(protect_mode.get("steps") or [])),
                        "protect_overshoot_mm": float((protect_mode.get("meta") or {}).get("overshoot_mm") or 0.0),
                    },
                    "part_cut_paths": part_cut_paths,
                    "equivalence_features": equivalence_features,
                    "debug": sheet.debug,
                }
            )

        self._profile_log(
            "run_summary",
            sheets_used=len(all_sheets),
            remaining=len(remaining),
            efficiency=efficiency,
            **self._profile_summary_fields(),
        )
        return {
            "sheets": sheets_payload,
            "sheets_used": len(all_sheets),
            "sheets_available": max_sheets,
            "unplaced_count": len(remaining),
            "efficiency_percent": round(efficiency, 2),
            "total_parts_area": sum(float(item["max_area"]) for item in expanded_parts),
            "total_used_area": total_used_area,
            "kerf_mm": self.kerf,
            "algorithm": "adaptive_frontier_release_v1",
            "diagnostics": {
                "placed_count": len(placed_ids),
                "remaining_count": len(remaining),
                "profile_summary": self._profile_summary_fields(),
                "target_dead_end": target_dead_end_info,
                "seed_summaries": seed_summaries,
                "cleanup_decisions": cleanup_decisions,
                "board_structure_profile": board_structure_profile,
            },
        }
