# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
from __future__ import annotations

from typing import Any

from grainline._internal.normalize import normalize_grain_direction


def board_axis_by_geometry(board_obj: Any) -> str | None:
    grain_axis = str(getattr(board_obj, "grain_axis", "none") or "none").strip().lower()
    if grain_axis == "none":
        return None
    length = float(getattr(board_obj, "length_mm", 0) or 0)
    width = float(getattr(board_obj, "width_mm", 0) or 0)
    long_is_length = length >= width
    if grain_axis == "long":
        return "length" if long_is_length else "width"
    if grain_axis == "short":
        return "width" if long_is_length else "length"
    return None


def grain_allows_orientation(board_obj: Any, part_obj: Any, *, rotated: bool) -> bool:
    axis = board_axis_by_geometry(board_obj)
    req = normalize_grain_direction(getattr(part_obj, "grain_direction", None), strict=False)
    if axis is None or req == "any":
        return True
    if req == "length":
        return (not rotated and axis == "length") or (rotated and axis == "width")
    if req == "width":
        return (not rotated and axis == "width") or (rotated and axis == "length")
    return True


def orientation_candidates(board_obj: Any, part_obj: Any) -> list[dict[str, Any]]:
    length = float(getattr(part_obj, "length_mm", 0) or 0)
    width = float(getattr(part_obj, "width_mm", 0) or 0)
    direct = {"width": length, "height": width, "rotated": False}
    rotated = {"width": width, "height": length, "rotated": True}
    axis = board_axis_by_geometry(board_obj)
    req = normalize_grain_direction(getattr(part_obj, "grain_direction", None), strict=False)

    if axis is None or req == "any":
        return [direct, rotated]
    if req == "length":
        return [direct] if axis == "length" else [rotated]
    if req == "width":
        return [direct] if axis == "width" else [rotated]
    return [direct, rotated]


def fit_modes_for_usable_area(
    board_obj: Any,
    part_obj: Any,
    *,
    usable_length: float,
    usable_width: float,
) -> tuple[bool, bool]:
    length = getattr(part_obj, "length_mm", None)
    width = getattr(part_obj, "width_mm", None)
    if length is None or width is None:
        return False, False
    length = float(length)
    width = float(width)
    direct_fit = (
        grain_allows_orientation(board_obj, part_obj, rotated=False)
        and length <= float(usable_length)
        and width <= float(usable_width)
    )
    rotated_fit = (
        grain_allows_orientation(board_obj, part_obj, rotated=True)
        and width <= float(usable_length)
        and length <= float(usable_width)
    )
    return direct_fit, rotated_fit
