# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple


def _component_indices(rects: List[Any], tol: float) -> List[List[int]]:
    remaining = set(range(len(rects)))
    components: List[List[int]] = []
    while remaining:
        start = remaining.pop()
        stack = [start]
        component = [start]
        while stack:
            idx = stack.pop()
            a = rects[idx]
            neighbours: List[int] = []
            for other_idx in list(remaining):
                b = rects[other_idx]
                overlap_x = min(a.right, b.right) - max(a.x, b.x)
                overlap_y = min(a.top, b.top) - max(a.y, b.y)
                horizontal_touch = overlap_y > -tol and (abs(a.right - b.x) <= tol or abs(b.right - a.x) <= tol)
                vertical_touch = overlap_x > -tol and (abs(a.top - b.y) <= tol or abs(b.top - a.y) <= tol)
                overlap_touch = overlap_x > -tol and overlap_y > -tol
                if horizontal_touch or vertical_touch or overlap_touch:
                    neighbours.append(other_idx)
            for other_idx in neighbours:
                remaining.remove(other_idx)
                component.append(other_idx)
                stack.append(other_idx)
        components.append(component)
    return components


def compute_main_mass_bbox(rects: Iterable[Any], *, tol: float) -> Optional[Tuple[float, float, float, float]]:
    rect_list = list(rects)
    if not rect_list:
        return None
    components = _component_indices(rect_list, tol)
    components.sort(
        key=lambda comp: (
            -sum(float(rect_list[idx].area) for idx in comp),
            -len(comp),
        )
    )
    main = components[0]
    left = min(rect_list[idx].x for idx in main)
    right = max(rect_list[idx].right for idx in main)
    bottom = min(rect_list[idx].y for idx in main)
    top = max(rect_list[idx].top for idx in main)
    return left, bottom, right, top


def _bbox_for_indices(rects: Sequence[Any], indices: Sequence[int]) -> Tuple[float, float, float, float]:
    left = min(rects[idx].x for idx in indices)
    right = max(rects[idx].right for idx in indices)
    bottom = min(rects[idx].y for idx in indices)
    top = max(rects[idx].top for idx in indices)
    return left, bottom, right, top


def compute_gravity_bbox(rects: Iterable[Any], *, tol: float) -> Optional[Tuple[float, float, float, float]]:
    rect_list = list(rects)
    if not rect_list:
        return None

    sorted_rects = sorted(rect_list, key=lambda rect: float(rect.area), reverse=True)
    total_area = sum(float(rect.area) for rect in sorted_rects)
    cumulative = 0.0
    heavy: List[Any] = []
    largest_area = float(sorted_rects[0].area)
    for rect in sorted_rects:
        area = float(rect.area)
        short_side = min(float(rect.width), float(rect.height))
        if area >= largest_area * 0.22 or short_side >= max(tol * 6.0, 120.0):
            heavy.append(rect)
            cumulative += area
        if cumulative >= total_area * 0.72:
            break

    if not heavy:
        heavy = sorted_rects[: max(1, min(4, len(sorted_rects)))]

    bbox = compute_main_mass_bbox(heavy, tol=tol)
    if bbox is not None:
        return bbox
    return compute_main_mass_bbox(rect_list, tol=tol)


def gap_to_bbox(rect: Any, bbox: Tuple[float, float, float, float]) -> float:
    left, bottom, right, top = bbox
    dx = 0.0
    if rect.right < left:
        dx = left - rect.right
    elif rect.x > right:
        dx = rect.x - right
    dy = 0.0
    if rect.top < bottom:
        dy = bottom - rect.top
    elif rect.y > top:
        dy = rect.y - top
    if dx <= 0.0:
        return max(0.0, dy)
    if dy <= 0.0:
        return max(0.0, dx)
    return dx + dy


def compact_components_toward_main_mass(
    rects: List[Any],
    *,
    kerf: float,
    sheet_length: float,
    sheet_width: float,
    tol: float,
) -> bool:
    if len(rects) <= 1:
        return False

    moved_any = False
    while True:
        components = _component_indices(rects, tol)
        if len(components) <= 1:
            break

        components.sort(
            key=lambda comp: (
                -sum(float(rects[idx].area) for idx in comp),
                -len(comp),
            )
        )
        main = components[0]
        main_bbox = _bbox_for_indices(rects, main)
        progress = False

        for comp in components[1:]:
            comp_bbox = _bbox_for_indices(rects, comp)
            direction, distance = _preferred_slide_direction(comp_bbox, main_bbox, kerf=kerf)
            if distance <= tol:
                continue
            delta = _max_component_shift(
                rects,
                component=comp,
                direction=direction,
                desired=distance,
                kerf=kerf,
                sheet_length=sheet_length,
                sheet_width=sheet_width,
                tol=tol,
            )
            if delta <= tol:
                continue
            _shift_component(rects, comp, direction=direction, delta=delta)
            moved_any = True
            progress = True
            break

        if not progress:
            break

    return moved_any


def _preferred_slide_direction(
    comp_bbox: Tuple[float, float, float, float],
    main_bbox: Tuple[float, float, float, float],
    *,
    kerf: float,
) -> Tuple[str, float]:
    comp_left, comp_bottom, comp_right, comp_top = comp_bbox
    main_left, main_bottom, main_right, main_top = main_bbox

    vertical_gap = 0.0
    vertical_dir = ""
    if comp_bottom >= main_top:
        vertical_gap = comp_bottom - main_top - kerf
        vertical_dir = "down"
    elif comp_top <= main_bottom:
        vertical_gap = main_bottom - comp_top - kerf
        vertical_dir = "up"

    horizontal_gap = 0.0
    horizontal_dir = ""
    if comp_left >= main_right:
        horizontal_gap = comp_left - main_right - kerf
        horizontal_dir = "left"
    elif comp_right <= main_left:
        horizontal_gap = main_left - comp_right - kerf
        horizontal_dir = "right"

    choices = [(vertical_gap, vertical_dir), (horizontal_gap, horizontal_dir)]
    choices = [(max(0.0, gap), direction) for gap, direction in choices if direction]
    if not choices:
        return "down", 0.0
    choices.sort(key=lambda item: (item[0], 0 if item[1] in {"down", "up"} else 1))
    gap, direction = choices[0]
    return direction, gap


def _max_component_shift(
    rects: Sequence[Any],
    *,
    component: Sequence[int],
    direction: str,
    desired: float,
    kerf: float,
    sheet_length: float,
    sheet_width: float,
    tol: float,
) -> float:
    component_set = set(component)
    limit = max(0.0, desired)

    if direction == "down":
        for idx in component:
            rect = rects[idx]
            limit = min(limit, rect.y)
            for other_idx, other in enumerate(rects):
                if other_idx in component_set:
                    continue
                overlap_x = min(rect.right, other.right) - max(rect.x, other.x)
                if overlap_x > -tol and other.top <= rect.y + tol:
                    limit = min(limit, rect.y - other.top - kerf)
    elif direction == "up":
        for idx in component:
            rect = rects[idx]
            limit = min(limit, sheet_width - rect.top)
            for other_idx, other in enumerate(rects):
                if other_idx in component_set:
                    continue
                overlap_x = min(rect.right, other.right) - max(rect.x, other.x)
                if overlap_x > -tol and other.y >= rect.top - tol:
                    limit = min(limit, other.y - rect.top - kerf)
    elif direction == "left":
        for idx in component:
            rect = rects[idx]
            limit = min(limit, rect.x)
            for other_idx, other in enumerate(rects):
                if other_idx in component_set:
                    continue
                overlap_y = min(rect.top, other.top) - max(rect.y, other.y)
                if overlap_y > -tol and other.right <= rect.x + tol:
                    limit = min(limit, rect.x - other.right - kerf)
    elif direction == "right":
        for idx in component:
            rect = rects[idx]
            limit = min(limit, sheet_length - rect.right)
            for other_idx, other in enumerate(rects):
                if other_idx in component_set:
                    continue
                overlap_y = min(rect.top, other.top) - max(rect.y, other.y)
                if overlap_y > -tol and other.x >= rect.right - tol:
                    limit = min(limit, other.x - rect.right - kerf)

    return max(0.0, limit)


def _shift_component(rects: Sequence[Any], component: Sequence[int], *, direction: str, delta: float) -> None:
    if delta <= 0.0:
        return
    for idx in component:
        rect = rects[idx]
        if direction == "down":
            rect.y -= delta
        elif direction == "up":
            rect.y += delta
        elif direction == "left":
            rect.x -= delta
        elif direction == "right":
            rect.x += delta


def attachment_score(
    rect: Any,
    placements: Iterable[Any],
    *,
    sheet_length: float,
    sheet_width: float,
    zone_left: float,
    zone_bottom: float,
    tol: float,
) -> Tuple[float, float, float, float]:
    placed = list(placements)
    if not placed:
        boundary_score = 0.0
        if rect.x <= tol or rect.y <= tol or abs(rect.right - sheet_length) <= tol or abs(rect.top - sheet_width) <= tol:
            boundary_score = -1.0
        return (0.0, boundary_score, rect.y, rect.x)

    shared_contact = 0.0
    touch_count = 0.0
    for other in placed:
        overlap_x = max(0.0, min(rect.right, other.right) - max(rect.x, other.x))
        overlap_y = max(0.0, min(rect.top, other.top) - max(rect.y, other.y))
        horizontal_touch = abs(rect.right - other.x) <= tol or abs(other.right - rect.x) <= tol
        vertical_touch = abs(rect.top - other.y) <= tol or abs(other.top - rect.y) <= tol
        if horizontal_touch and overlap_y > tol:
            touch_count += 1.0
            shared_contact = max(shared_contact, overlap_y)
        if vertical_touch and overlap_x > tol:
            touch_count += 1.0
            shared_contact = max(shared_contact, overlap_x)

    bbox = compute_gravity_bbox(placed, tol=tol)
    gap = gap_to_bbox(rect, bbox) if bbox is not None else 0.0

    boundary_score = 0.0
    if (
        rect.x <= tol
        or rect.y <= tol
        or abs(rect.right - sheet_length) <= tol
        or abs(rect.top - sheet_width) <= tol
        or abs(rect.x - zone_left) <= tol
        or abs(rect.y - zone_bottom) <= tol
    ):
        boundary_score = -1.0

    return (gap, -touch_count, -shared_contact, boundary_score)
