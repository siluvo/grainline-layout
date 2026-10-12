# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Solve every benchmark case with Grainline and with unmodified CutStock, then compare.

CutStock (https://github.com/orohde/CutStock) is not redistributed here. Download
its core/optimize.py yourself and pass the path:

    python benchmarks/compare_cutstock.py --cutstock path/to/optimize.py

The file is checked against the SHA-256 the published results were produced with.
Pass --any-version to compare against a different revision.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import platform
import sys
import time
from pathlib import Path

import grainline
from grainline import SearchOptions, optimize, validate
from grainline.model import cut_proxy, load_problem, usable
from grainline._internal.grain_fit_rules import orientation_candidates
from grainline._internal.optimization import _strict_guillotine_metrics

HERE = Path(__file__).resolve().parent
CUTSTOCK_SHA256 = '77962978adb43562ef52b656bbc4a6ba664d303d71e19ddf5d099dc2c99c9cd5'
TOL = 1e-3


def largest_offcut(rects, length, width):
    """Area of the largest empty axis-aligned rectangle left on one sheet."""
    lefts = sorted({0.0, *[x + w for x, _, w, _ in rects]})
    rights = sorted({length, *[x for x, _, _, _ in rects]})
    best = 0.0
    for left in lefts:
        for right in rights:
            if right - left <= TOL:
                continue
            cursor = gap = 0.0
            for low, high in sorted((y, y + h) for x, y, w, h in rects if x < right - TOL and x + w > left + TOL):
                gap = max(gap, low - cursor)
                cursor = max(cursor, high)
            best = max(best, max(gap, width - cursor) * (right - left))
    return best


def check_geometry(sheets, board, parts, kerf):
    """Multiplicity, size and grain, bounds, kerf separation and guillotine separability."""
    length, width = usable(board)
    allowed = {part.id: orientation_candidates(board, cut_proxy(part)) for part in parts}
    placed = dict.fromkeys(allowed, 0)
    errors = []
    for number, sheet in enumerate(sheets, start=1):
        for rect in sheet:
            placed[rect['part_id']] += 1
            if not any(abs(rect['width'] - c['width']) < TOL and abs(rect['height'] - c['height']) < TOL
                       for c in allowed[rect['part_id']]):
                errors.append(f'sheet {number}: size or grain not allowed for {rect["part_id"]}')
            if (rect['x'] < -TOL or rect['y'] < -TOL or rect['x'] + rect['width'] > length + TOL
                    or rect['y'] + rect['height'] > width + TOL):
                errors.append(f'sheet {number}: {rect["part_id"]} outside the usable area')
        for index, a in enumerate(sheet):
            for b in sheet[index + 1:]:
                gap_x = max(a['x'], b['x']) - min(a['x'] + a['width'], b['x'] + b['width'])
                gap_y = max(a['y'], b['y']) - min(a['y'] + a['height'], b['y'] + b['height'])
                if max(gap_x, gap_y) < kerf - TOL:
                    errors.append(f'sheet {number}: parts closer than one kerf')
        if not _strict_guillotine_metrics(sheet, sheet_length=length, sheet_width=width)['is_guillotine']:
            errors.append(f'sheet {number}: not guillotine-cuttable')
    errors.extend(f'{part.id}: placed {placed[part.id]} of {part.quantity}'
                  for part in parts if placed[part.id] != part.quantity)
    return errors


def measure(sheets, length, width):
    rects = [[(r['x'], r['y'], r['width'], r['height']) for r in sheet] for sheet in sheets]
    return {'sheets': len(sheets),
            'largest_offcut_m2': round(max(largest_offcut(sheet, length, width) for sheet in rects) / 1e6, 3)}


def run_cutstock(algorithm, module, board, parts, kerf):
    length, width = usable(board)
    demand, rotation = [], {}
    for part in parts:
        proxy = cut_proxy(part)
        candidates = orientation_candidates(board, proxy)
        demand.append((part.id, proxy.length_mm, proxy.width_mm, part.quantity))
        rotation[part.id] = (any(not c['rotated'] for c in candidates), any(c['rotated'] for c in candidates))
    # One stock sheet per demanded piece is a safe upper bound on inventory.
    stock = module.PlattenVorrat(1, length, width, sum(part.quantity for part in parts))
    started = time.perf_counter()
    out = algorithm(demand, [stock], kerf=kerf, teil_drehung=rotation)
    seconds = time.perf_counter() - started
    sheets = [[{'part_id': p.teil_label, 'x': float(p.x), 'y': float(p.y),
                'width': float(p.laenge), 'height': float(p.breite)} for p in plan.platzierungen]
              for plan in out.schnittplaene]
    row = measure(sheets, length, width)
    row.update(seconds=round(seconds, 3), errors=check_geometry(sheets, board, parts, kerf))
    if out.fehlende_teile:
        row['errors'].append(f'{len(out.fehlende_teile)} parts not placed')
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cutstock', type=Path, required=True, help='path to CutStock core/optimize.py')
    parser.add_argument('--any-version', action='store_true', help='skip the SHA-256 check')
    parser.add_argument('--cases', type=Path, default=HERE / 'cases')
    parser.add_argument('--output', type=Path, default=HERE / 'results' / 'cutstock.json')
    args = parser.parse_args()

    digest = hashlib.sha256(args.cutstock.read_bytes()).hexdigest()
    if digest != CUTSTOCK_SHA256 and not args.any_version:
        parser.error(f'{args.cutstock} has SHA-256 {digest}, expected {CUTSTOCK_SHA256}')
    spec = importlib.util.spec_from_file_location('cutstock_optimize', args.cutstock)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    solvers = {'cutstock_nested': module.optimize_2d_nested, 'cutstock_ga': module.optimize_2d_ga}

    rows = []
    for path in sorted(args.cases.glob('*.json')):
        problem = json.loads(path.read_text(encoding='utf-8'))
        parts, boards, kerf = load_problem(problem)
        started = time.perf_counter()
        result = optimize(problem, options=SearchOptions.from_preset('balanced'))
        seconds = time.perf_counter() - started
        errors = validate(problem, result)
        for group in result['boards']:
            board = boards[group['board_id']]
            members = [part for part in parts if part.board_id == board.id]
            length, width = usable(board)
            area = sum(cut_proxy(p).length_mm * cut_proxy(p).width_mm * p.quantity for p in members)
            row = {'case': path.stem, 'category': problem.get('category'), 'material': board.code,
                   'parts': sum(part.quantity for part in members),
                   'area_lower_bound': math.ceil(area / (length * width) - 1e-9),
                   'grainline': measure([layout['placements'] for layout in group['layouts']], length, width)}
            row['grainline'].update(case_seconds=round(seconds, 3), errors=errors)
            for name, algorithm in solvers.items():
                row[name] = run_cutstock(algorithm, module, board, members, kerf)
            rows.append(row)
            print(row['case'], row['material'], 'lower bound', row['area_lower_bound'],
                  *[f"{name} {row[name]['sheets']}" for name in ('grainline', *solvers)], flush=True)

    report = {'grainline_version': grainline.__version__, 'preset': 'balanced',
              'cutstock_sha256': digest, 'python': platform.python_version(),
              'machine': platform.processor() or platform.machine(), 'rows': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + '\n', encoding='utf-8', newline='\n')
    return int(any(row[name]['errors'] for row in rows for name in ('grainline', *solvers)))


if __name__ == '__main__':
    raise SystemExit(main())
