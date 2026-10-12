"""Validate an external cut-list library against an existing sheet-count baseline."""
import argparse
import json
from pathlib import Path
import time

from grainline import SearchOptions, optimize, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases', type=Path)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--preset', choices=['fast', 'balanced', 'thorough'], default='balanced')
    parser.add_argument('--case', action='append', help='Only these case IDs (repeatable)')
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    failures = []
    paths = sorted(args.cases.glob('*.json'))
    if args.case:
        paths = [path for path in paths if path.stem in args.case]
    if not paths:
        parser.error('no cases found')
    for path in paths:
        problem = json.loads(path.read_text(encoding='utf-8'))
        started = time.perf_counter()
        try:
            result = optimize(problem, options=SearchOptions.from_preset(args.preset))
            errors = validate(problem, result)
            previous = baseline[problem['id']]['boards']
            for board in result['boards']:
                if board['required_sheets'] > previous[board['board_id']]['grainline_sheets']:
                    errors.append(f"{board['board_id']}: sheet-count regression")
            print(json.dumps({'case': path.stem, 'seconds': round(time.perf_counter()-started, 3),
                              'sheets': {b['board_id']: b['required_sheets'] for b in result['boards']},
                              'errors': errors}), flush=True)
            if errors:
                failures.append(path.stem)
        except (ValueError, RuntimeError, KeyError) as exc:
            failures.append(path.stem)
            print(json.dumps({'case': path.stem, 'error': str(exc)}), flush=True)
    print(json.dumps({'cases': len(paths), 'failures': failures}), flush=True)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
