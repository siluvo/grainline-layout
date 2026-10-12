# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Validate the portable JSON contract and adapt it to the extracted engine."""
from copy import copy
import math
from types import SimpleNamespace

from ._internal.banding import build_part_banding_payload
from ._internal.grain_fit_rules import orientation_candidates
from ._internal.normalize import normalize_grain_direction


def number(value, where: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(f"{where} must be finite and {'nonnegative' if zero else 'positive'}")
    return value


def usable(board) -> tuple[float, float]:
    return (board.length_mm - board.trim_left - board.trim_right,
            board.width_mm - board.trim_top - board.trim_bottom)


def cut_proxy(part):
    proxy = copy(part)
    proxy.length_mm = part.banding_payload['cut_length_mm']
    proxy.width_mm = part.banding_payload['cut_width_mm']
    return proxy


def load_problem(problem: dict):
    if not isinstance(problem, dict):
        raise ValueError("input must be a JSON object")
    if problem.get("units", "mm") != "mm":
        raise ValueError("units must be mm; convert imperial dimensions before calling")
    kerf = number(problem.get("kerf_mm", 3.0), "kerf_mm", zero=True)
    boards = {}
    if not isinstance(problem.get("boards"), list) or not problem["boards"]:
        raise ValueError("boards must be a nonempty array")
    for index, row in enumerate(problem["boards"]):
        if not isinstance(row, dict):
            raise ValueError(f"boards[{index}] must be an object")
        code = row.get("code")
        if not isinstance(code, str) or not code.strip() or code in boards:
            raise ValueError(f"boards[{index}].code must be a unique nonempty string")
        axis = row.get("grain_axis", "none")
        if axis not in ("long", "short", "none"):
            raise ValueError(f"board {code}: grain_axis must be long, short, or none")
        trim = row.get("trim_mm", {})
        if not isinstance(trim, dict) or set(trim) - {"left", "right", "top", "bottom"}:
            raise ValueError(f"board {code}: trim_mm accepts left, right, top, bottom")
        board = SimpleNamespace(
            id=code, code=code, name=str(row.get("name", code)),
            length_mm=number(row.get("length_mm"), f"board {code}.length_mm"),
            width_mm=number(row.get("width_mm"), f"board {code}.width_mm"),
            thickness_mm=number(row.get("thickness_mm", 18), f"board {code}.thickness_mm"),
            grain_axis=axis,
            **{f"trim_{side}": number(trim.get(side, 0), f"board {code}.trim_mm.{side}", zero=True)
               for side in ("left", "right", "top", "bottom")},
        )
        if min(usable(board)) <= 0:
            raise ValueError(f"board {code}: trim leaves no usable area")
        boards[code] = board
    if not isinstance(problem.get("parts"), list) or not problem["parts"]:
        raise ValueError("parts must be a nonempty array")
    parts, seen, count = [], set(), 0
    for index, row in enumerate(problem["parts"]):
        if not isinstance(row, dict):
            raise ValueError(f"parts[{index}] must be an object")
        identifier = row.get("id", f"p{index}")
        if not isinstance(identifier, str) or not identifier.strip() or ':' in identifier or identifier in seen:
            raise ValueError(f"parts[{index}].id must be unique, nonempty, and contain no ':'")
        seen.add(identifier)
        board_id = row.get("board")
        if not isinstance(board_id, str) or board_id not in boards:
            raise ValueError(f"part {identifier}: board must reference a board code")
        quantity = row.get("quantity", 1)
        if type(quantity) is not int or quantity < 1:
            raise ValueError(f"part {identifier}: quantity must be a positive integer")
        count += quantity
        if count > 500:
            raise ValueError("at most 500 parts after quantity expansion are supported")
        grain = row.get("grain_direction", "any")
        if grain is not None and not isinstance(grain, str):
            raise ValueError(f"part {identifier}: grain_direction must be length, width, or any")
        grain = normalize_grain_direction(grain, strict=True)
        part = SimpleNamespace(
            id=identifier, name=str(row.get("name", identifier)), project_id=None,
            board_id=board_id, quantity=quantity, grain_direction=grain,
            length_mm=number(row.get("length_mm"), f"part {identifier}.length_mm"),
            width_mm=number(row.get("width_mm"), f"part {identifier}.width_mm"),
            thickness_mm=boards[board_id].thickness_mm,
        )
        payload = build_part_banding_payload(part, {})
        if ("cut_length_mm" in row) != ("cut_width_mm" in row):
            raise ValueError(f"part {identifier}: provide both cut_length_mm and cut_width_mm")
        if "cut_length_mm" in row:
            length = number(row["cut_length_mm"], f"part {identifier}.cut_length_mm")
            width = number(row["cut_width_mm"], f"part {identifier}.cut_width_mm")
            if length > part.length_mm or width > part.width_mm:
                raise ValueError(f"part {identifier}: cut sizes must not exceed finished sizes")
            payload.update(has_banding=True, cut_length_mm=length, cut_width_mm=width,
                           decrement_length_mm=part.length_mm - length,
                           decrement_width_mm=part.width_mm - width)
        part.banding_payload = payload
        length, width = usable(boards[board_id])
        if not any(c['width'] <= length and c['height'] <= width
                   for c in orientation_candidates(boards[board_id], cut_proxy(part))):
            raise ValueError(f"part {identifier}: cannot fit usable board with the specified grain")
        parts.append(part)
    return parts, boards, kerf
