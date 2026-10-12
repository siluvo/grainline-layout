# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""Cheap search for a split of parts across a fixed number of sheets.

The frontier algorithm fills one sheet at a time and never revisits a finished sheet, so it can
miss a sheet count that only works when hard parts and small fillers are shared out deliberately.
This search answers one narrow question quickly: can these parts be divided over `sheet_count`
sheets with guillotine cuts? It scores nothing about cut flow or offcut quality. The caller takes
the split and lets the frontier engine lay out each sheet.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

EPS = 1e-6


@dataclass
class PlannedPlacement:
    part_id: str
    x: float
    y: float
    width: float
    height: float
    rotated: bool
    host: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass
class PlannedSheet:
    placements: List[PlannedPlacement] = field(default_factory=list)
    free_rects: List[Tuple[float, float, float, float]] = field(default_factory=list)
    # True while every split on this sheet matches the frontier engine's bottom-left guillotine split,
    # which is what the engine's isolation cut recorder can replay.
    engine_split: bool = True

    @property
    def part_ids(self) -> set[str]:
        return {item.part_id for item in self.placements}

    @property
    def used_area(self) -> float:
        return sum(item.width * item.height for item in self.placements)


def _pack_once(
    pieces: Sequence[Tuple[str, Sequence[Dict[str, Any]], float]],
    sheet_count: int,
    sheet_length: float,
    sheet_width: float,
    kerf: float,
    rng: random.Random,
) -> List[PlannedSheet] | None:
    order = sorted(pieces, key=lambda piece: -piece[2] * rng.uniform(0.6, 1.4))
    sheets = [PlannedSheet(free_rects=[(0.0, 0.0, sheet_length, sheet_width)]) for _ in range(sheet_count)]
    for part_id, candidates, _area in order:
        best = None
        for sheet_index, sheet in enumerate(sheets):
            for slot, (_x, _y, free_w, free_h) in enumerate(sheet.free_rects):
                for cand in candidates:
                    width, height = float(cand["width"]), float(cand["height"])
                    if width <= free_w + EPS and height <= free_h + EPS:
                        score = (min(free_w - width, free_h - height), free_w * free_h, sheet_index)
                        if best is None or score < best[0]:
                            best = (score, sheet_index, slot, cand)
        if best is None:
            return None
        _score, sheet_index, slot, cand = best
        sheet = sheets[sheet_index]
        x, y, free_w, free_h = sheet.free_rects.pop(slot)
        width, height = float(cand["width"]), float(cand["height"])
        sheet.placements.append(
            PlannedPlacement(part_id, x, y, width, height, bool(cand.get("rotated", False)), host=(x, y, free_w, free_h))
        )
        right, below = free_w - width - kerf, free_h - height - kerf
        # Usually keep the larger leftover whole; sometimes split the other way to diversify.
        horizontal_first = (right * free_h < below * free_w) if rng.random() < 0.7 else (rng.random() < 0.5)
        if horizontal_first or right <= EPS or below <= EPS:
            # The cut above the part runs the full width of the host (the engine's own split).
            if right > EPS:
                sheet.free_rects.append((x + width + kerf, y, right, height))
            if below > EPS:
                sheet.free_rects.append((x, y + height + kerf, free_w, below))
        else:
            sheet.engine_split = False
            sheet.free_rects.append((x + width + kerf, y, right, free_h))
            sheet.free_rects.append((x, y + height + kerf, width, below))
    return [sheet for sheet in sheets if sheet.placements]


def find_sheet_plans(
    pieces: Sequence[Tuple[str, Sequence[Dict[str, Any]], float]],
    *,
    sheet_count: int,
    sheet_length: float,
    sheet_width: float,
    kerf: float,
    time_budget_s: float | None,
    max_tries: int,
    max_plans: int,
    seed: int = 20261011,
    stats: Dict[str, Any] | None = None,
) -> List[List[PlannedSheet]]:
    """Return up to `max_plans` distinct splits, tightest-front-sheets first.

    `pieces` is a list of (part_id, orientation candidates, area). Each candidate needs `width`,
    `height` and `rotated`. Stops at the first of: time budget, try limit, enough plans.
    """
    rng = random.Random(seed + sheet_count)
    deadline = None if time_budget_s is None else time.perf_counter() + max(0.0, float(time_budget_s))
    plans: List[List[PlannedSheet]] = []
    seen: set[frozenset] = set()
    tried = 0
    stop_reason = "trial_limit"
    for _ in range(max(0, int(max_tries))):
        if (deadline is not None and time.perf_counter() > deadline) or len(plans) >= max_plans:
            stop_reason = "plan_limit" if len(plans) >= max_plans else "time_limit"
            break
        tried += 1
        sheets = _pack_once(pieces, sheet_count, sheet_length, sheet_width, kerf, rng)
        if not sheets:
            continue
        signature = frozenset(frozenset(sheet.part_ids) for sheet in sheets)
        if signature in seen:
            continue
        seen.add(signature)
        sheets.sort(key=lambda sheet: -sheet.used_area)
        plans.append(sheets)
    if stats is not None:
        stats.update(trials_executed=tried, plans_found=len(plans), search_stop_reason=stop_reason)
    # Prefer plans every sheet of which the engine can replay, then plans that pack the front sheets
    # tight and gather the free area on the last one.
    plans.sort(
        key=lambda sheets: (
            sum(1 for sheet in sheets if not sheet.engine_split),
            -sum(sheet.used_area ** 2 for sheet in sheets),
        )
    )
    return plans
