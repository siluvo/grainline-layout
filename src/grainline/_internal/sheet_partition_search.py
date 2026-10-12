# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""Cheap search for a split of parts across a fixed number of sheets.

The frontier algorithm fills one sheet at a time and never revisits a finished sheet, so it can
miss a sheet count that only works when hard parts and small fillers are shared out deliberately.
This search answers one narrow question quickly: can these parts be divided over `sheet_count`
sheets with guillotine cuts? It scores nothing about cut flow or offcut quality. The caller takes
the split and lets the frontier engine lay out each sheet.

Two searches run in turn. The first places pieces one at a time in a random order. When it finds
nothing, the second samples full single-sheet patterns and picks a set that holds every piece.

Two searches run in turn. The first places pieces one at a time in a random order. When it finds
nothing, the second samples full single-sheet patterns and picks a set that holds every piece.
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


# Sheet patterns kept for the covering step, fullest first, and the size of that search.
PATTERN_KEEP = 2000
PATTERN_BRANCH = 8
PATTERN_NODE_LIMIT = 400


def _sample_pattern(
    kinds: Sequence[Sequence[Dict[str, Any]]],
    demand: Sequence[int],
    sheet_length: float,
    sheet_width: float,
    kerf: float,
    rng: random.Random,
) -> Tuple[Tuple[int, ...], PlannedSheet, List[int]]:
    """Fill one sheet slot by slot, choosing a piece for each free rectangle.

    `_pack_once` takes pieces in a fixed order and finds each a slot, which rarely builds a full
    sheet out of rows and stacks of equal parts. Here the slot comes first, a piece that closes a
    slot edge is favoured, and the bias toward large pieces changes from one sample to the next.
    Returns how many of each kind were placed, the sheet, and the kind of every placement.
    """
    remaining = list(demand)
    power = rng.choice((0.0, 0.5, 1.0, 2.0))
    edge_bonus = rng.choice((1.0, 3.0, 10.0))
    sheet = PlannedSheet(engine_split=False)
    placed_kinds: List[int] = []
    free = [(0.0, 0.0, sheet_length, sheet_width)]
    while free:
        x, y, free_w, free_h = free.pop()
        best = None
        for kind, candidates in enumerate(kinds):
            if not remaining[kind]:
                continue
            for cand in candidates:
                width, height = float(cand["width"]), float(cand["height"])
                if width <= free_w + EPS and height <= free_h + EPS:
                    score = (width * height) ** power * rng.random()
                    if free_w - width < kerf or free_h - height < kerf:
                        score *= edge_bonus
                    if best is None or score > best[0]:
                        best = (score, kind, cand)
        if best is None:
            sheet.free_rects.append((x, y, free_w, free_h))
            continue
        _score, kind, cand = best
        remaining[kind] -= 1
        placed_kinds.append(kind)
        width, height = float(cand["width"]), float(cand["height"])
        sheet.placements.append(
            PlannedPlacement("", x, y, width, height, bool(cand.get("rotated", False)), host=(x, y, free_w, free_h))
        )
        right, below = free_w - width - kerf, free_h - height - kerf
        if right > EPS and below > EPS:
            if rng.random() < 0.5:
                free.append((x, y + height + kerf, free_w, below))
                free.append((x + width + kerf, y, right, height))
            else:
                free.append((x + width + kerf, y, right, free_h))
                free.append((x, y + height + kerf, width, below))
        elif right > EPS:
            free.append((x + width + kerf, y, right, free_h))
        elif below > EPS:
            free.append((x, y + height + kerf, free_w, below))
    counts = [0] * len(demand)
    for kind in placed_kinds:
        counts[kind] += 1
    return tuple(counts), sheet, placed_kinds


def _cover_demand(
    patterns: Sequence[Tuple[int, ...]],
    demand: Sequence[int],
    areas: Sequence[float],
    sheet_count: int,
    stats: Dict[str, Any],
) -> List[int] | None:
    """Pick at most `sheet_count` patterns, repeats allowed, that together hold every piece."""
    nodes = 0

    def search(need: List[int], sheets_left: int) -> List[int] | None:
        nonlocal nodes
        if all(count <= 0 for count in need):
            return []
        if sheets_left == 0 or nodes >= PATTERN_NODE_LIMIT:
            return None
        nodes += 1
        need_area = sum(count * area for count, area in zip(need, areas) if count > 0)
        ranked = sorted(
            (
                (sum(min(have, count) * area for have, count, area in zip(pattern, need, areas) if count > 0), index)
                for index, pattern in enumerate(patterns)
            ),
            reverse=True,
        )
        for gain, index in ranked[:PATTERN_BRANCH]:
            # Not even this much on every sheet left would hold what is still needed.
            if gain * sheets_left < need_area - EPS:
                break
            rest = search([count - have for count, have in zip(need, patterns[index])], sheets_left - 1)
            if rest is not None:
                return [index] + rest
        return None

    chosen = search(list(demand), sheet_count)
    stats["cover_nodes"] = nodes
    return chosen


def _find_pattern_plan(
    pieces: Sequence[Tuple[str, Sequence[Dict[str, Any]], float]],
    *,
    sheet_count: int,
    sheet_length: float,
    sheet_width: float,
    kerf: float,
    deadline: float | None,
    max_samples: int,
    rng: random.Random,
    stats: Dict[str, Any],
) -> List[PlannedSheet] | None:
    """Sample many single-sheet patterns, then choose `sheet_count` of them that hold all pieces.

    Equal pieces are counted together, so a pattern is just a count per kind and the same pattern
    can be used for several sheets. A chosen pattern may hold more of a kind than is still needed;
    the spare places are left empty.
    """
    kind_of: Dict[Tuple[Tuple[float, float, bool], ...], int] = {}
    kinds: List[Sequence[Dict[str, Any]]] = []
    members: List[List[str]] = []
    for part_id, candidates, _area in pieces:
        key = tuple((float(c["width"]), float(c["height"]), bool(c.get("rotated", False))) for c in candidates)
        if key not in kind_of:
            kind_of[key] = len(kinds)
            kinds.append(candidates)
            members.append([])
        members[kind_of[key]].append(part_id)
    demand = [len(ids) for ids in members]
    areas = [float(candidates[0]["width"]) * float(candidates[0]["height"]) for candidates in kinds]

    found: Dict[Tuple[int, ...], Tuple[PlannedSheet, List[int]]] = {}
    sampled = 0
    for sampled in range(1, max(0, int(max_samples)) + 1):
        if deadline is not None and sampled % 64 == 0 and time.perf_counter() > deadline:
            break
        counts, sheet, placed_kinds = _sample_pattern(kinds, demand, sheet_length, sheet_width, kerf, rng)
        found.setdefault(counts, (sheet, placed_kinds))
    patterns = sorted(found, key=lambda counts: -sum(count * area for count, area in zip(counts, areas)))[:PATTERN_KEEP]
    stats.update(pattern_samples=sampled, patterns_found=len(found))
    chosen = _cover_demand(patterns, demand, areas, sheet_count, stats)
    if chosen is None:
        return None

    pools = [list(reversed(ids)) for ids in members]
    plan: List[PlannedSheet] = []
    for index in chosen:
        template, placed_kinds = found[patterns[index]]
        sheet = PlannedSheet(free_rects=list(template.free_rects), engine_split=False)
        for item, kind in zip(template.placements, placed_kinds):
            if pools[kind]:
                sheet.placements.append(
                    PlannedPlacement(pools[kind].pop(), item.x, item.y, item.width, item.height, item.rotated, host=item.host)
                )
            else:
                sheet.free_rects.append((item.x, item.y, item.width, item.height))
        if sheet.placements:
            plan.append(sheet)
    plan.sort(key=lambda sheet: -sheet.used_area)
    return plan


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
    pattern_stats: Dict[str, Any] = {}
    if not plans and stop_reason == "trial_limit" and max_plans > 0:
        # A full trial packs every sheet, a pattern sample packs one, so this is about the same work again.
        plan = _find_pattern_plan(
            pieces,
            sheet_count=sheet_count,
            sheet_length=sheet_length,
            sheet_width=sheet_width,
            kerf=kerf,
            deadline=deadline,
            max_samples=tried * sheet_count,
            rng=random.Random(seed * 31 + sheet_count),
            stats=pattern_stats,
        )
        if plan is not None:
            plans.append(plan)
    if stats is not None:
        stats.update(trials_executed=tried, plans_found=len(plans), search_stop_reason=stop_reason, **pattern_stats)
    # Prefer plans every sheet of which the engine can replay, then plans that pack the front sheets
    # tight and gather the free area on the last one.
    plans.sort(
        key=lambda sheets: (
            sum(1 for sheet in sheets if not sheet.engine_split),
            -sum(sheet.used_area ** 2 for sheet in sheets),
        )
    )
    return plans
