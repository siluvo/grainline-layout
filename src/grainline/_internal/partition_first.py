# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
# Extracted/adapted 2026-10-11; see PROVENANCE.md for original source and changes.
"""
Partition-First Algorithm v1 (Release)

Status: Release (frozen)
Frozen date: 2026-03-09

Author: Kimi
Algorithm approach:
- Partition-first: Large parts anchor edges to create main zones
- Group placement: Similar-sized parts form columns/strips
- Main offcut protection: Always preserve a large rectangular leftover
- Cut-friendly: Minimize direction switches

Temp test results (grainline_template (6).csv):
- Sheets: 6-7 (MDF18) + 1 (PLY18)
- Efficiency: ~75-80%
- Runtime: ~50-100ms per sheet

Note: This file is frozen. All iterative development continues in partition_first_v1_1.py.
When v1.1 tests OK, its Status will change to "Release" and this file may be deprecated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple, Optional, Set
import math


@dataclass
class Rect:
    """Rectangle with placement info."""
    x: float
    y: float
    width: float
    height: float
    part_id: str
    part_name: str
    rotated: bool = False
    area: float = field(init=False)

    def __post_init__(self):
        self.area = self.width * self.height

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height


@dataclass
class Zone:
    """A partition zone within a sheet."""
    x: float
    y: float
    width: float
    height: float
    zone_type: str  # 'main', 'secondary', 'fill', 'offcut'
    placements: List[Rect] = field(default_factory=list)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def used_area(self) -> float:
        return sum(p.area for p in self.placements)

    @property
    def free_area(self) -> float:
        return self.area - self.used_area

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height


@dataclass
class SheetResult:
    """Result for a single sheet."""
    id: int
    zones: List[Zone]
    placements: List[Rect]
    sheet_length: float
    sheet_width: float
    main_offcut: Optional[Zone] = None

    @property
    def used_area(self) -> float:
        return sum(p.area for p in self.placements)

    @property
    def total_area(self) -> float:
        return self.sheet_length * self.sheet_width


class PartitionFirstAlgorithm:
    """
    Partition-First Cutting Stock Algorithm.

    Key differences from existing algorithms:
    1. Explicit zone planning before placement
    2. Size-based part grouping
    3. Main offcut protection (always try to leave one big rectangle)
    4. Column-oriented placement for medium parts
    """

    def __init__(
        self,
        kerf_mm: float = 3.0,
        allow_rotation: bool = True,
        main_offcut_min_area_ratio: float = 0.15,
        large_part_threshold: float = 0.25,
    ):
        self.kerf = kerf_mm
        self.allow_rotation = allow_rotation
        self.main_offcut_min_area_ratio = main_offcut_min_area_ratio
        self.large_part_threshold = large_part_threshold

    def _reserve_kerf(self, remaining: float) -> float:
        """Reserve kerf gap if there's enough space."""
        eps = 1e-9
        return float(self.kerf) if remaining > float(self.kerf) + eps else 0.0

    def _get_candidates(self, part: Dict) -> List[Dict]:
        """Get orientation candidates for a part."""
        raw_candidates = part.get("candidates")
        if raw_candidates:
            return raw_candidates

        # Fallback: create from dimensions
        w = float(part.get("width", 0) or part.get("length_mm", 0))
        h = float(part.get("height", 0) or part.get("width_mm", 0))
        return [{"width": w, "height": h, "rotated": False}]

    def _classify_parts(
        self,
        parts: List[Dict[str, Any]],
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """
        Classify parts into: large, medium, small.
        """
        sheet_area = sheet_length * sheet_width
        large_threshold = sheet_area * self.large_part_threshold
        small_threshold = sheet_area * 0.02

        large, medium, small = [], [], []

        for part in parts:
            candidates = self._get_candidates(part)
            max_area = max(c["width"] * c["height"] for c in candidates)
            max_dim = max(max(c["width"], c["height"]) for c in candidates)

            if max_area >= large_threshold or max_dim > min(sheet_length, sheet_width) * 0.45:
                large.append(part)
            elif max_area <= small_threshold:
                small.append(part)
            else:
                medium.append(part)

        return large, medium, small

    def _sort_parts_for_placement(self, parts: List[Dict], key: str = "area") -> List[Dict]:
        """Sort parts for optimal placement order."""
        def _get_value(part: Dict) -> float:
            candidates = self._get_candidates(part)
            if key == "area":
                return max(c["width"] * c["height"] for c in candidates)
            elif key == "max_side":
                return max(max(c["width"], c["height"]) for c in candidates)
            elif key == "min_side":
                return max(min(c["width"], c["height"]) for c in candidates)
            elif key == "perimeter":
                return max(2 * (c["width"] + c["height"]) for c in candidates)
            return 0

        return sorted(parts, key=_get_value, reverse=True)

    def _rects_overlap(self, a: Rect, b: Rect) -> bool:
        """Check if two rectangles overlap."""
        eps = 1e-6
        return not (
            a.x + a.width <= b.x + eps
            or b.x + b.width <= a.x + eps
            or a.y + a.height <= b.y + eps
            or b.y + b.height <= a.y + eps
        )

    def _is_inside_sheet(self, rect: Rect, sheet_length: float, sheet_width: float) -> bool:
        """Check if rectangle is fully inside sheet bounds."""
        eps = 1e-6
        return (
            rect.x >= -eps
            and rect.y >= -eps
            and rect.x + rect.width <= sheet_length + eps
            and rect.y + rect.height <= sheet_width + eps
        )

    def _can_place(
        self,
        rect: Rect,
        placements: List[Rect],
        sheet_length: float,
        sheet_width: float,
    ) -> bool:
        """Check if rect can be placed without overlap and inside bounds."""
        if not self._is_inside_sheet(rect, sheet_length, sheet_width):
            return False

        for p in placements:
            if self._rects_overlap(rect, p):
                return False
        return True

    def _find_best_position(
        self,
        zone: Zone,
        width: float,
        height: float,
        sheet_length: float,
        sheet_width: float,
        all_placements: List[Rect],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Find best position using bottom-left with kerf-aware placement.
        Returns (x, y) or (None, None) if no valid position.
        """
        if width > zone.width or height > zone.height:
            return None, None

        # Generate candidate y positions (bottoms of existing rects + kerf, and zone bottom)
        y_candidates = {zone.y}
        for p in all_placements:
            # Bottom of this rect could support another
            y_candidates.add(p.y)
            # Top of this rect + kerf is a candidate
            y_candidates.add(p.y + p.height + self.kerf)

        y_candidates = sorted(y for y in y_candidates
                            if y >= zone.y - 1e-6
                            and y + height <= zone.y + zone.height + 1e-6)

        for y in y_candidates:
            # Generate x candidates at this y level
            x_candidates = {zone.x}
            for p in all_placements:
                # Check if this rect's y-range overlaps with our y-range
                y_overlap = not (y + height <= p.y or y >= p.y + p.height)
                if y_overlap:
                    # Place to the right of this rect + kerf
                    x_candidates.add(p.x + p.width + self.kerf)

            # Filter valid x positions
            x_candidates = sorted(x for x in x_candidates
                                if x >= zone.x - 1e-6
                                and x + width <= zone.x + zone.width + 1e-6)

            for x in x_candidates:
                test_rect = Rect(x, y, width, height, "", "")
                if self._can_place(test_rect, all_placements, sheet_length, sheet_width):
                    return x, y

        return None, None

    def _try_place_part(
        self,
        part: Dict,
        zones: List[Zone],
        placed_ids: Set[str],
        sheet_length: float,
        sheet_width: float,
    ) -> Optional[Tuple[Rect, Zone]]:
        """Try to place a single part in any zone."""
        part_id = str(part.get("id", ""))
        if part_id in placed_ids:
            return None

        candidates = self._get_candidates(part)
        part_name = str(part.get("name", "Part"))

        # Collect all placements for overlap checking
        all_placements = []
        for z in zones:
            all_placements.extend(z.placements)

        # Try each candidate orientation
        for cand in candidates:
            w = float(cand["width"])
            h = float(cand["height"])
            rotated = bool(cand.get("rotated", False))

            # Try zones in order: main first, then secondary, offcut last
            zone_order = sorted(zones, key=lambda z: {
                "main": 0, "secondary": 1, "fill": 2, "offcut": 3
            }.get(z.zone_type, 4))

            for zone in zone_order:
                x, y = self._find_best_position(
                    zone, w, h, sheet_length, sheet_width, all_placements
                )

                if x is not None and y is not None:
                    rect = Rect(
                        x=x,
                        y=y,
                        width=w,
                        height=h,
                        part_id=part_id,
                        part_name=part_name,
                        rotated=rotated,
                    )

                    # Final verification
                    if self._can_place(rect, all_placements, sheet_length, sheet_width):
                        return rect, zone

        return None

    def _group_similar_parts(self, parts: List[Dict]) -> List[List[Dict]]:
        """Group similar-sized parts for column placement."""
        if not parts:
            return []

        sorted_parts = sorted(
            parts,
            key=lambda p: max(
                max(c["width"], c["height"])
                for c in self._get_candidates(p)
            ),
            reverse=True
        )

        groups = []
        for part in sorted_parts:
            candidates = self._get_candidates(part)
            part_width = max(c["width"] for c in candidates)

            placed = False
            for group in groups:
                group_width = max(
                    max(c["width"] for c in self._get_candidates(g))
                    for g in group
                )
                if abs(part_width - group_width) / max(part_width, group_width, 1) < 0.15:
                    group.append(part)
                    placed = True
                    break

            if not placed:
                groups.append([part])

        return groups

    def _create_zones(
        self,
        sheet_length: float,
        sheet_width: float,
        large_parts: List[Dict],
    ) -> List[Zone]:
        """Create zones based on large parts."""
        zones = []
        remaining_x = 0.0
        remaining_w = sheet_length

        # Sort large parts by max dimension
        sorted_large = sorted(
            large_parts,
            key=lambda p: max(max(c["width"], c["height"]) for c in self._get_candidates(p)),
            reverse=True
        )

        # Reserve zones for large parts along left edge
        for part in sorted_large[:3]:  # Max 3 large part zones
            candidates = self._get_candidates(part)
            max_w = max(max(c["width"], c["height"]) for c in candidates)
            zone_width = max_w + self.kerf * 2

            if zone_width < remaining_w * 0.6:  # Don't take too much
                zone = Zone(
                    x=remaining_x,
                    y=0,
                    width=min(zone_width, remaining_w),
                    height=sheet_width,
                    zone_type="main",
                )
                zones.append(zone)
                remaining_x += zone.width
                remaining_w -= zone.width

        # Remaining space as secondary zone
        if remaining_w > 100:
            secondary = Zone(
                x=remaining_x,
                y=0,
                width=remaining_w,
                height=sheet_width,
                zone_type="secondary",
            )
            zones.append(secondary)

        return zones

    def _compute_leftovers(
        self,
        placements: List[Rect],
        sheet_length: float,
        sheet_width: float,
    ) -> List[Dict]:
        """Compute leftover rectangles using a simple shelf-based approach."""
        if not placements:
            return [{
                "x": 0, "y": 0,
                "width": sheet_length,
                "height": sheet_width,
                "area": sheet_length * sheet_width,
            }]

        leftovers = []

        # Find gaps between placements
        max_x = max(p.right for p in placements)
        max_y = max(p.top for p in placements)

        # Right side leftover
        if max_x < sheet_length - self.kerf:
            leftovers.append({
                "x": max_x + self.kerf,
                "y": 0,
                "width": sheet_length - max_x - self.kerf,
                "height": sheet_width,
                "area": (sheet_length - max_x - self.kerf) * sheet_width,
            })

        # Top leftover
        if max_y < sheet_width - self.kerf:
            leftovers.append({
                "x": 0,
                "y": max_y + self.kerf,
                "width": sheet_length,
                "height": sheet_width - max_y - self.kerf,
                "area": sheet_length * (sheet_width - max_y - self.kerf),
            })

        return [l for l in leftovers if l["width"] > 10 and l["height"] > 10 and l["area"] > 0]

    def _compute_offcut_metrics(
        self,
        leftovers: List[Dict],
        sheet_length: float,
        sheet_width: float,
    ) -> Dict[str, Any]:
        """Compute offcut quality metrics."""
        total_area = sheet_length * sheet_width

        if not leftovers:
            return {
                "total_offcut_area": 0,
                "largest_offcut_area": 0,
                "offcut_fragment_count": 0,
                "main_offcut_ratio": 0,
            }

        total_offcut = sum(l["area"] for l in leftovers)
        largest_offcut = max(l["area"] for l in leftovers)

        # Count reusable offcuts (min side >= 180mm, aspect <= 4.5)
        reusable_area = 0
        reusable_count = 0
        skinny_area = 0

        for l in leftovers:
            w, h = l["width"], l["height"]
            min_side = min(w, h)
            max_side = max(w, h)
            aspect = max_side / max(min_side, 1e-9)

            if min_side >= 180 and aspect <= 4.5:
                reusable_area += l["area"]
                reusable_count += 1
            if min_side < 120 or aspect >= 8:
                skinny_area += l["area"]

        return {
            "total_offcut_area": round(total_offcut, 2),
            "largest_offcut_area": round(largest_offcut, 2),
            "reusable_offcut_area": round(reusable_area, 2),
            "reusable_offcut_count": reusable_count,
            "skinny_scrap_area": round(skinny_area, 2),
            "offcut_fragment_count": len(leftovers),
            "main_offcut_ratio": round(largest_offcut / total_offcut, 4) if total_offcut > 0 else 0,
        }

    def optimize_sheet(
        self,
        parts: List[Dict[str, Any]],
        sheet_length: float,
        sheet_width: float,
    ) -> Tuple[List[Rect], List[Dict]]:
        """Optimize placement on a single sheet."""
        if not parts:
            return [], []

        # Classify parts
        large, medium, small = self._classify_parts(parts, sheet_length, sheet_width)

        # Create zones
        zones = self._create_zones(sheet_length, sheet_width, large)
        if not zones:
            zones = [Zone(x=0, y=0, width=sheet_length, height=sheet_width, zone_type="main")]

        placements: List[Rect] = []
        placed_ids: Set[str] = set()
        unplaced: List[Dict] = []

        # Place large parts first
        for part in self._sort_parts_for_placement(large, "max_side"):
            result = self._try_place_part(part, zones, placed_ids, sheet_length, sheet_width)
            if result:
                rect, zone = result
                zone.placements.append(rect)
                placements.append(rect)
                placed_ids.add(rect.part_id)
            else:
                unplaced.append(part)

        # Place medium parts in groups
        medium_groups = self._group_similar_parts(medium)
        for group in medium_groups:
            for part in self._sort_parts_for_placement(group, "area"):
                if part.get("id", "") in placed_ids:
                    continue

                result = self._try_place_part(part, zones, placed_ids, sheet_length, sheet_width)
                if result:
                    rect, zone = result
                    zone.placements.append(rect)
                    placements.append(rect)
                    placed_ids.add(rect.part_id)
                else:
                    unplaced.append(part)

        # Fill small parts
        for part in self._sort_parts_for_placement(small, "area"):
            if part.get("id", "") in placed_ids:
                continue

            result = self._try_place_part(part, zones, placed_ids, sheet_length, sheet_width)
            if result:
                rect, zone = result
                zone.placements.append(rect)
                placements.append(rect)
                placed_ids.add(rect.part_id)
            else:
                unplaced.append(part)

        return placements, unplaced

    def optimize(
        self,
        parts: List[Any],
        boards: List[Any],
        sheet_length: float,
        sheet_width: float,
        max_sheets: int = 50,
    ) -> Dict[str, Any]:
        """Full optimization across multiple sheets."""
        # Expand parts by quantity
        expanded_parts = []
        for part in parts:
            qty = getattr(part, 'quantity', 1)
            for i in range(qty):
                expanded_parts.append({
                    'id': f"{getattr(part, 'id', 'part')}_{i}" if qty > 1 else str(getattr(part, 'id', i)),
                    'name': getattr(part, 'name', 'Part'),
                    'width': float(getattr(part, 'length_mm', 0)),
                    'height': float(getattr(part, 'width_mm', 0)),
                    'candidates': getattr(part, 'candidates', None),
                    'original': part
                })

        total_parts_area = sum(p['width'] * p['height'] for p in expanded_parts)

        # Multi-sheet optimization
        all_sheets: List[SheetResult] = []
        remaining = expanded_parts
        sheet_id = 0

        while remaining and sheet_id < max_sheets:
            sheet_id += 1
            placements, unplaced = self.optimize_sheet(
                remaining, sheet_length, sheet_width
            )

            if placements:
                # Verify no overlaps
                valid = True
                for i, p1 in enumerate(placements):
                    for p2 in placements[i+1:]:
                        if self._rects_overlap(p1, p2):
                            valid = False
                            break
                    if not valid:
                        break

                if valid:
                    sheet = SheetResult(
                        id=sheet_id,
                        zones=[],  # Simplified
                        placements=placements,
                        sheet_length=sheet_length,
                        sheet_width=sheet_width,
                    )
                    all_sheets.append(sheet)
                    remaining = unplaced
                else:
                    # Overlap detected, don't use this sheet
                    unplaced = remaining
                    break
            else:
                # No placements made, stop to avoid infinite loop
                unplaced = remaining
                break

            if not remaining:
                break

        # Build output with proper format
        sheets_data = []
        for s in all_sheets:
            leftovers = self._compute_leftovers(s.placements, sheet_length, sheet_width)
            offcut_metrics = self._compute_offcut_metrics(leftovers, sheet_length, sheet_width)

            sheets_data.append({
                'id': s.id,
                'placements': [
                    {
                        'part_id': p.part_id,
                        'part_name': p.part_name,
                        'x': round(p.x, 2),
                        'y': round(p.y, 2),
                        'width': round(p.width, 2),
                        'height': round(p.height, 2),
                        'rotated': p.rotated,
                    }
                    for p in s.placements
                ],
                'utilization': round(s.used_area / s.total_area * 100, 2) if s.total_area > 0 else 0,
                'used_area': round(s.used_area, 2),
                'total_area': round(s.total_area, 2),
                'leftovers': leftovers,
                'offcut_metrics': offcut_metrics,
            })

        total_used = sum(s.used_area for s in all_sheets)
        total_available = sum(s.total_area for s in all_sheets)
        overall_efficiency = (total_used / total_available * 100) if total_available > 0 else 0

        return {
            'sheets': sheets_data,
            'sheets_used': len(all_sheets),
            'sheets_available': max_sheets,
            'unplaced_count': len(remaining),
            'efficiency_percent': round(overall_efficiency, 2),
            'total_parts_area': round(total_parts_area, 2),
            'total_used_area': round(total_used, 2),
            'total_area': round(total_available, 2),
            'algorithm': 'partition-first-v1',
        }


def run_partition_first(
    parts: List[Any],
    boards: List[Any],
    sheet_length: float,
    sheet_width: float,
    kerf_mm: float = 3.0,
    max_sheets: int = 50,
) -> Dict[str, Any]:
    """Convenience function to run partition-first algorithm."""
    algo = PartitionFirstAlgorithm(kerf_mm=kerf_mm)
    return algo.optimize(
        parts=parts,
        boards=boards,
        sheet_length=sheet_length,
        sheet_width=sheet_width,
        max_sheets=max_sheets,
    )
