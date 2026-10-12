# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Siluvo LLC
"""Grainline's standalone API: optimize, validate and SearchOptions."""
from .options import SearchOptions

__version__ = "0.1.0.dev0"


def optimize(problem: dict, *, options: SearchOptions | None = None) -> dict:
    """Optimize a JSON-compatible cut list without network, files or app services.

    An explicit options object replaces problem['options']. Geometry is validated
    before returning. Incomplete results are explicitly marked, never called optimal.
    """
    from dataclasses import fields
    import hashlib
    import json
    from .model import load_problem
    from ._internal.optimization import run_optimization
    from .validation import validate

    parts, boards, kerf = load_problem(problem)
    if options is None:
        raw = problem.get('options', {})
        if not isinstance(raw, dict):
            raise ValueError('options must be an object')
        unknown = set(raw) - {f.name for f in fields(SearchOptions)} - {'preset'}
        if unknown:
            raise ValueError(f'unknown search options: {", ".join(sorted(unknown))}')
        overrides = {key: value for key, value in raw.items() if key != 'preset'}
        options = SearchOptions.from_preset(raw.get('preset', 'balanced'), **overrides)
    if not isinstance(options, SearchOptions):
        raise ValueError('options must be a SearchOptions instance')
    result = run_optimization(parts, list(boards.values()), kerf_mm=kerf,
                              algorithm='adaptive_frontier_release_v1', options=options)
    errors = validate(problem, result, require_complete=False)
    if errors:
        raise RuntimeError('optimizer produced an invalid layout: ' + '; '.join(errors[:8]))
    result['schema_version'] = 1
    result['engine_version'] = __version__
    result['status'] = 'complete' if result['unplaced_count'] == 0 else 'incomplete'
    result['validation'] = {
        'geometry_valid': True,
        'complete': result['status'] == 'complete',
        'cut_plan_available_for_all_sheets': all(
            bool(((sheet.get('cut_plan_meta') or {}).get('validation') or {}).get('passed'))
            for group in result['boards'] for sheet in group['layouts']
        ),
    }
    result['search'] = {
        'options': options.to_dict(),
        'time_limit_reached': bool(result.get('timed_out', False)),
        'stop_reason': ('time_limit' if result.get('timed_out') else
                        'completed' if result['status'] == 'complete' else 'sheet_limit'),
        'reproducible_mode': options.time_limit is None and options.partition_time_limit is None,
        'input_sha256': hashlib.sha256(json.dumps(problem, sort_keys=True, separators=(',', ':'),
                                                 ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
    }
    return result


def validate(problem: dict, result: dict, *, require_complete: bool = True) -> list[str]:
    from .validation import validate as validate_result
    return validate_result(problem, result, require_complete=require_complete)


__all__ = ['optimize', 'validate', 'SearchOptions', '__version__']
