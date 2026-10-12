# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""JSON on stdout; human-readable errors on stderr. No network access."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import SearchOptions, __version__, optimize, validate


def _read(path: str):
    text = sys.stdin.read() if path == '-' else Path(path).read_text(encoding='utf-8-sig')
    return json.loads(text)


def _write(path: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if path == '-':
        sys.stdout.write(text)
        return
    target = Path(path)
    # Do not leave a half-written result when interrupted.
    fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    # Pipes on Windows otherwise inherit a legacy code page.
    for stream, encoding in ((sys.stdin, 'utf-8-sig'), (sys.stdout, 'utf-8'), (sys.stderr, 'utf-8')):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding=encoding)
    parser = argparse.ArgumentParser(prog='grainline', description='Local grain-aware sheet cutting plans')
    parser.add_argument('--version', action='version', version=f'grainline {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    solve = commands.add_parser('optimize', help='Read a cut list and write a JSON cutting plan')
    solve.add_argument('input', help='UTF-8 JSON file, or - for stdin')
    solve.add_argument('-o', '--output', default='-', help='Output JSON file (default: stdout)')
    solve.add_argument('--preset', choices=['fast', 'balanced', 'thorough'])
    for flag, help_text in [
        ('seed-limit', 'Number of starting pieces explored per sheet (>=2)'),
        ('candidate-pool-limit', 'Candidate piece pool size (>=8)'),
        ('random-seed', 'Seed for randomized fixed-sheet partition search'),
        ('partition-tries', 'Maximum partition trials per target; 0 disables fallback'),
        ('partition-plans', 'Maximum distinct partition plans per target'),
        ('max-sheets', 'Sheet cap per material group'),
    ]:
        solve.add_argument('--' + flag, type=int, help=help_text)
    solve.add_argument('--time-limit', type=float, help='Soft total time budget in seconds; atomic work may finish later')
    solve.add_argument('--partition-time-limit', type=float, help='Soft fallback time budget per material, in seconds')
    solve.add_argument('--seed-early-stop', action=argparse.BooleanOptionalAction, default=None,
                       help='Stop seed search after repeated duplicate layouts')
    check = commands.add_parser('validate', help='Check a plan against its original cut list')
    check.add_argument('input')
    check.add_argument('result')
    check.add_argument('--allow-incomplete', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            if args.input == args.result == '-':
                raise ValueError('only one input may use stdin')
            errors = validate(_read(args.input), _read(args.result), require_complete=not args.allow_incomplete)
            _write('-', {'valid': not errors, 'errors': errors})
            return 0 if not errors else 3
        problem = _read(args.input)
        if not isinstance(problem, dict):
            raise ValueError('input must be a JSON object')
        embedded = problem.get('options', {})
        if not isinstance(embedded, dict):
            raise ValueError('options must be an object')
        overrides = {key: value for key, value in vars(args).items()
                     if key not in ('command', 'input', 'output') and value is not None}
        # CLI overrides individual JSON options; a preset establishes defaults only.
        problem = {**problem, 'options': {**embedded, **overrides}}
        if args.output != '-' and args.input != '-' and Path(args.input).resolve() == Path(args.output).resolve():
            raise ValueError('output must not overwrite the input cut list')
        result = optimize(problem)
        _write(args.output, result)
        return 0 if result['status'] == 'complete' else 3
    except (ValueError, TypeError, OSError) as exc:
        print(f'grainline: {exc}', file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f'grainline: {exc}', file=sys.stderr)
        return 4
