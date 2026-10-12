# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Solve every benchmark case with PackingSolver and compare with Grainline.

PackingSolver (https://github.com/fontanf/packingsolver) is not redistributed
here. Download a release, then point this script at its guillotine solver:

    python benchmarks/compare_packingsolver.py --solver path/to/packingsolver_rectangleguillotine

Each material is one bin-packing instance with unlimited cut stages, the case's
kerf as cut thickness, and rotation allowed only where the grain rules allow it.
"""
import argparse
import csv
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path

import grainline
from grainline import SearchOptions, optimize, validate
from grainline.model import cut_proxy, load_problem, usable
from grainline._internal.grain_fit_rules import orientation_candidates
from grainline._internal.optimization import _strict_guillotine_metrics

from compare_cutstock import largest_offcut, measure

HERE = Path(__file__).resolve().parent
SCALE = 100  # PackingSolver takes integer lengths; this keeps hundredths of a millimetre.
ROUNDING = 1.5 / SCALE


def run_packingsolver(solver, folder, board, parts, kerf, time_limit):
    length, width = usable(board)
    allowed = [orientation_candidates(board, cut_proxy(part)) for part in parts]
    with (folder / 'items.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['WIDTH', 'HEIGHT', 'COPIES', 'ORIENTED'])
        for part, candidates in zip(parts, allowed):
            writer.writerow([round(candidates[0]['width'] * SCALE), round(candidates[0]['height'] * SCALE),
                             part.quantity, int(len(candidates) == 1)])
    with (folder / 'bins.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['WIDTH', 'HEIGHT', 'COPIES'])
        writer.writerow([round(length * SCALE), round(width * SCALE), sum(part.quantity for part in parts)])
    started = time.perf_counter()
    subprocess.run([str(solver), '-i', 'items.csv', '-b', 'bins.csv', '--objective', 'bin-packing',
                    '--number-of-stages-unlimited', '--cut-thickness', str(round(kerf * SCALE)),
                    '-t', str(time_limit), '-c', 'certificate.csv', '-o', 'output.json', '-v', '0'],
                   cwd=folder, check=True, capture_output=True)
    seconds = time.perf_counter() - started
    output = json.loads((folder / 'output.json').read_text())['Output']
    with (folder / 'certificate.csv').open(newline='') as stream:
        nodes = list(csv.DictReader(stream))
    parents = {node['PARENT'] for node in nodes}
    plates = {}
    for node in nodes:
        plate = plates.setdefault(node['PLATE_ID'], {'copies': int(node['COPIES']), 'nodes': 0, 'items': []})
        plate['nodes'] += 1
        if int(node['TYPE']) >= 0 and node['NODE_ID'] not in parents:
            plate['items'].append((int(node['TYPE']), node['PARENT'] == '',
                                   {key: int(node[label]) / SCALE for key, label in
                                    (('x', 'X'), ('y', 'Y'), ('width', 'WIDTH'), ('height', 'HEIGHT'))}))
    errors, sheets, placed = [], [], [0] * len(parts)
    for plate in plates.values():
        # A root node carries type 0 whether or not it is an item; it is one only when it stands alone.
        items = [(kind, rect) for kind, is_root, rect in plate['items'] if not is_root or plate['nodes'] == 1]
        sheets.extend([rect for _, rect in items] for _ in range(plate['copies']))
        for kind, rect in items:
            placed[kind] += plate['copies']
            if not any(abs(rect['width'] - c['width']) < ROUNDING and abs(rect['height'] - c['height']) < ROUNDING
                       for c in allowed[kind]):
                errors.append('size or grain not allowed')
            if (rect['x'] < -ROUNDING or rect['y'] < -ROUNDING or rect['x'] + rect['width'] > length + ROUNDING
                    or rect['y'] + rect['height'] > width + ROUNDING):
                errors.append('part outside the usable area')
        rects = [rect for _, rect in items]
        for index, a in enumerate(rects):
            for b in rects[index + 1:]:
                gap_x = max(a['x'], b['x']) - min(a['x'] + a['width'], b['x'] + b['width'])
                gap_y = max(a['y'], b['y']) - min(a['y'] + a['height'], b['y'] + b['height'])
                if max(gap_x, gap_y) < kerf - ROUNDING:
                    errors.append('parts closer than one kerf')
        if not _strict_guillotine_metrics(rects, sheet_length=length, sheet_width=width)['is_guillotine']:
            errors.append('not guillotine-cuttable')
    errors.extend(f'{part.id}: placed {count} of {part.quantity}'
                  for part, count in zip(parts, placed) if count != part.quantity)
    row = measure(sheets, length, width)
    row.update(proven_lower_bound=output['BinPackingBound'], seconds=round(seconds, 2), errors=sorted(set(errors)))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--solver', type=Path, required=True, help='packingsolver_rectangleguillotine executable')
    parser.add_argument('--time-limit', type=float, default=10.0, help='seconds per material (default 10)')
    parser.add_argument('--cases', type=Path, default=HERE / 'cases')
    parser.add_argument('--output', type=Path, default=HERE / 'results' / 'packingsolver.json')
    args = parser.parse_args()

    rows = []
    with tempfile.TemporaryDirectory() as temp:
        for path in sorted(args.cases.glob('*.json')):
            problem = json.loads(path.read_text(encoding='utf-8'))
            parts, boards, kerf = load_problem(problem)
            started = time.perf_counter()
            result = optimize(problem, options=SearchOptions.from_preset('balanced'))
            seconds = time.perf_counter() - started
            errors = validate(problem, result)
            for number, group in enumerate(result['boards']):
                board = boards[group['board_id']]
                members = [part for part in parts if part.board_id == board.id]
                length, width = usable(board)
                area = sum(cut_proxy(p).length_mm * cut_proxy(p).width_mm * p.quantity for p in members)
                row = {'case': path.stem, 'material': board.code, 'parts': sum(p.quantity for p in members),
                       'area_lower_bound': math.ceil(area / (length * width) - 1e-9),
                       'grainline': measure([layout['placements'] for layout in group['layouts']], length, width)}
                row['grainline'].update(case_seconds=round(seconds, 3), errors=errors)
                folder = Path(temp) / f'{path.stem}-{number}'
                folder.mkdir()
                row['packingsolver'] = run_packingsolver(args.solver.resolve(), folder, board, members, kerf, args.time_limit)
                rows.append(row)
                print(row['case'], row['material'], 'grainline', row['grainline']['sheets'],
                      'packingsolver', row['packingsolver']['sheets'],
                      'proven lower bound', row['packingsolver']['proven_lower_bound'],
                      *row['packingsolver']['errors'], flush=True)

    report = {'grainline_version': grainline.__version__, 'preset': 'balanced',
              'packingsolver_time_limit_s': args.time_limit, 'rows': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + '\n', encoding='utf-8', newline='\n')
    print('sheets: grainline', sum(row['grainline']['sheets'] for row in rows),
          'packingsolver', sum(row['packingsolver']['sheets'] for row in rows),
          'proven lower bound', sum(row['packingsolver']['proven_lower_bound'] for row in rows))
    return int(any(row[name]['errors'] for row in rows for name in ('grainline', 'packingsolver')))


if __name__ == '__main__':
    raise SystemExit(main())
