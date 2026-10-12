# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Replay default through-cuts; geometry feasibility alone is not a cut plan."""
from __future__ import annotations

import math
from typing import Any, Sequence

TOL = 1e-4


def validate_cut_plan(
    placements: Sequence[dict[str, Any]],
    steps: Sequence[dict[str, Any]],
    *,
    sheet_length: float,
    sheet_width: float,
    kerf_mm: float,
) -> dict[str, Any]:
    """Check the default rectangular through-cut sequence and final part release.

    A kerf may consume the whole edge strip (one surviving child). A zero-cut
    plan is valid only if every placed part is already an entire stock workpiece.
    This is a geometric replay, not certification of a physical machine operation.
    """
    errors: list[str] = []

    def rect(value: Any) -> tuple[float, float, float, float]:
        if not isinstance(value, dict):
            raise ValueError('rectangle must be an object')
        numbers = tuple(value.get(key) for key in ('x', 'y', 'width', 'height'))
        if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in numbers):
            raise ValueError('rectangle coordinates must be finite numbers')
        return tuple(float(n) for n in numbers)

    def same(a, b) -> bool:
        return all(abs(x - y) <= TOL for x, y in zip(a, b))

    # (rectangle, producer-assigned workpiece ID, producing step ID)
    stock = (0.0, 0.0, float(sheet_length), float(sheet_width))
    live = [(stock, None, None)]
    seen_steps: set[str] = set()
    seen_workpieces: set[str] = set()
    part_rects = [rect(p) for p in placements]
    for index, step in enumerate(steps):
        where = f'cut {index + 1}'
        try:
            if not isinstance(step, dict):
                raise ValueError('step must be an object')
            step_id = step.get('id')
            if not isinstance(step_id, str) or not step_id or step_id in seen_steps:
                raise ValueError('missing or duplicate step ID')
            before = rect(step.get('workpiece_before'))
            match = next((i for i, item in enumerate(live) if same(item[0], before)), None)
            if match is None:
                raise ValueError('workpiece does not exist or was already cut')
            _, workpiece_id, producer = live[match]
            if workpiece_id is not None and step.get('workpiece_before_id') != workpiece_id:
                raise ValueError('workpiece ID does not match its producer')
            dependencies = step.get('depends_on_step_ids', [])
            if not isinstance(dependencies, list) or any(d not in seen_steps for d in dependencies):
                raise ValueError('dependency has not run')
            if producer is not None and producer not in dependencies:
                raise ValueError('missing workpiece dependency')
            band = rect(step.get('cut_band'))
            axis = step.get('axis')
            x, y, width, height = before
            bx, by, bw, bh = band
            if axis == 'vertical':
                if abs(by - y) > TOL or abs(bh - height) > TOL or abs(bw - kerf_mm) > TOL:
                    raise ValueError('vertical band does not span the workpiece with the specified kerf')
                if bx >= x + width - TOL or bx + bw <= x + TOL:
                    raise ValueError('vertical cut does not intersect the stock')
                right = max(x, bx + bw)
                children = [(x, y, max(0.0, bx - x), height),
                            (right, y, max(0.0, x + width - right), height)]
            elif axis == 'horizontal':
                if abs(bx - x) > TOL or abs(bw - width) > TOL or abs(bh - kerf_mm) > TOL:
                    raise ValueError('horizontal band does not span the workpiece with the specified kerf')
                if by >= y + height - TOL or by + bh <= y + TOL:
                    raise ValueError('horizontal cut does not intersect the stock')
                top = max(y, by + bh)
                children = [(x, y, width, max(0.0, by - y)),
                            (x, top, width, max(0.0, y + height - top))]
            else:
                raise ValueError('unknown cut axis')
            if step.get('through_cut') is not True or abs(float(step.get('kerf_mm', -1)) - kerf_mm) > TOL:
                raise ValueError('invalid cut type or kerf metadata')
            expected_start = (bx + bw * 0.5, by) if axis == 'vertical' else (bx, by + bh * 0.5)
            expected_end = (bx + bw * 0.5, by + bh) if axis == 'vertical' else (bx + bw, by + bh * 0.5)
            for field, expected in (('start', expected_start), ('end', expected_end)):
                point = step.get(field)
                if not isinstance(point, dict) or any(
                    isinstance(point.get(key), bool) or not isinstance(point.get(key), (float, int))
                    or not math.isfinite(point[key]) or abs(point[key] - value) > TOL
                    for key, value in zip(('x', 'y'), expected)
                ):
                    raise ValueError('tool centerline does not match the blade band')
            children = [c for c in children if c[2] > TOL and c[3] > TOL]
            if not children or any(same(c, before) for c in children):
                raise ValueError('cut does not produce smaller workpieces')
            for px, py, pw, ph in part_rects:
                overlap_x = min(bx + bw, px + pw) - max(bx, px)
                overlap_y = min(by + bh, py + ph) - max(by, py)
                hits = overlap_x > TOL and overlap_y > TOL
                if kerf_mm == 0:
                    hits = ((axis == 'vertical' and px + TOL < bx < px + pw - TOL and overlap_y > TOL)
                            or (axis == 'horizontal' and py + TOL < by < py + ph - TOL and overlap_x > TOL))
                if hits:
                    raise ValueError('blade band cuts through a finished part')
            after = step.get('workpieces_after')
            after_ids = step.get('workpiece_after_ids')
            if not isinstance(after, list) or not isinstance(after_ids, list) or len(after) != len(after_ids):
                raise ValueError('invalid child workpieces')
            actual = [rect(item) for item in after]
            if len(actual) != len(children) or not all(same(a, b) for a, b in zip(sorted(actual), sorted(children))):
                raise ValueError('child geometry does not match cutting the parent')
            if any(not isinstance(i, str) or not i or i in seen_workpieces for i in after_ids) or len(set(after_ids)) != len(after_ids):
                raise ValueError('duplicate or invalid child workpiece ID')
            live.pop(match)
            live.extend((r, identifier, step_id) for r, identifier in zip(actual, after_ids))
            seen_steps.add(step_id)
            seen_workpieces.update(after_ids)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f'{where}: {exc}')
            break
    unreleased = [str(p.get('part_id') or '') for p, r in zip(placements, part_rects)
                  if not any(same(r, item[0]) for item in live)]
    if unreleased:
        errors.append('parts not isolated: ' + ', '.join(unreleased))
    return {'passed': not errors, 'errors': errors, 'step_count': len(steps),
            'unreleased_part_ids': unreleased,
            'cut_required': any(not same(r, stock) for r in part_rects)}
