# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Geometry validation independent of the optimizer's reported validity flags."""
from collections import Counter
import math

from .model import load_problem, cut_proxy, usable
from ._internal.grain_fit_rules import orientation_candidates
from ._internal.optimization import _strict_guillotine_metrics
from ._internal.cut_plan_validation import validate_cut_plan

TOL = 1e-3


def validate(problem: dict, result: dict, *, require_complete: bool = True) -> list[str]:
    """Return violations of multiplicity, dimensions, grain, bounds, kerf or slicing.

    This checks guillotine geometry, not physical machine/toolpath safety.
    """
    parts, boards, kerf = load_problem(problem)
    specs = {part.id: part for part in parts}
    placed = Counter()
    expected_instances = {
        (f'{part.id}:{index}' if part.quantity > 1 else part.id)
        for part in parts for index in range(part.quantity)
    }
    instance_ids = set()
    errors = []
    if not isinstance(result, dict) or not isinstance(result.get('boards'), list):
        return ['result.boards must be an array']
    for group in result['boards']:
        if not isinstance(group, dict) or group.get('board_id') not in boards:
            errors.append('unknown board in result')
            continue
        board = boards[group['board_id']]
        length, width = usable(board)
        layouts = group.get('layouts')
        if not isinstance(layouts, list):
            errors.append(f'{board.code}: layouts must be an array')
            continue
        for index, sheet in enumerate(layouts):
            where = f'{board.code} sheet {index + 1}'
            if not isinstance(sheet, dict) or not isinstance(sheet.get('placements'), list):
                errors.append(f'{where}: placements must be an array')
                continue
            rects = sheet['placements']
            well_formed = True
            for rect in rects:
                if not isinstance(rect, dict) or any(
                    isinstance(rect.get(key), bool) or not isinstance(rect.get(key), (int, float))
                    or not math.isfinite(rect[key]) for key in ('x', 'y', 'width', 'height')
                ):
                    errors.append(f'{where}: invalid rectangle')
                    well_formed = False
                    continue
                instance_id = str(rect.get('part_id', ''))
                identifier = instance_id.split(':')[0]
                if instance_id not in expected_instances or instance_id in instance_ids:
                    errors.append(f'{where}: unknown or duplicate part instance {instance_id}')
                instance_ids.add(instance_id)
                if identifier not in specs:
                    errors.append(f'{where}: unknown part {identifier}')
                    continue
                part = specs[identifier]
                placed[identifier] += 1
                if part.board_id != board.id:
                    errors.append(f'{where}: wrong material for {identifier}')
                allowed = orientation_candidates(board, cut_proxy(part))
                if type(rect.get('rotated', False)) is not bool or not any(abs(rect['width'] - c['width']) < TOL and abs(rect['height'] - c['height']) < TOL
                           and rect.get('rotated', False) == c['rotated'] for c in allowed):
                    errors.append(f'{where}: incorrect dimensions or grain for {identifier}')
                if rect['x'] < -TOL or rect['y'] < -TOL or rect['x'] + rect['width'] > length + TOL or rect['y'] + rect['height'] > width + TOL:
                    errors.append(f'{where}: out of bounds')
            if not well_formed:
                continue
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    gap_x = max(a['x'], b['x']) - min(a['x'] + a['width'], b['x'] + b['width'])
                    gap_y = max(a['y'], b['y']) - min(a['y'] + a['height'], b['y'] + b['height'])
                    if max(gap_x, gap_y) < kerf - TOL:
                        errors.append(f'{where}: overlap or insufficient kerf')
            if not _strict_guillotine_metrics(rects, sheet_length=length, sheet_width=width)['is_guillotine']:
                errors.append(f'{where}: not guillotine-cuttable')
            plan = validate_cut_plan(rects, sheet.get('cut_plan_steps') or [],
                                     sheet_length=length, sheet_width=width, kerf_mm=kerf)
            errors.extend(f'{where}: {error}' for error in plan['errors'])
    for identifier, part in specs.items():
        if placed[identifier] > part.quantity or (require_complete and placed[identifier] != part.quantity):
            errors.append(f'{identifier}: placed {placed[identifier]} of {part.quantity}')
    missing = sum(max(0, part.quantity - placed[part.id]) for part in parts)
    if result.get('unplaced_count') != missing:
        errors.append('unplaced_count does not match placements')
    if result.get('sheets_used') != sum(len(g.get('layouts', [])) for g in result['boards'] if isinstance(g, dict) and isinstance(g.get('layouts'), list)):
        errors.append('sheets_used does not match layouts')
    return errors
