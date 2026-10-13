# Extraction provenance

Extracted on 2026-10-11 from the closed-source Grainline web application
repository, commit `da4f67a16f0ebb364006f86c848679a73053b55a`.

| Source path in the web application | Package implementation |
|---|---|
| `backend/app/services/algorithms/adaptive_frontier_release_v1.py` | `_internal/adaptive_frontier_release_v1.py` |
| `backend/app/services/algorithms/sheet_partition_search.py` | `_internal/sheet_partition_search.py` |
| `backend/app/services/algorithms/placement_affinity.py` | `_internal/placement_affinity.py` |
| `backend/app/services/algorithms/partition_first.py` | `_internal/partition_first.py` (cleanup candidate) |
| `backend/app/services/optimization.py` | `_internal/optimization.py` (frontier orchestration, shared geometry and cleanup helpers) |
| `backend/app/services/banding.py` | `_internal/banding.py` |
| `backend/app/services/grain_fit_rules.py` | `_internal/grain_fit_rules.py` |
| `backend/app/core/normalize.py` | `_internal/normalize.py` |

Changes: package-local imports; no application settings, environment loading or
request-log writes; a standard logging sink; explicit per-call budgets; deterministic
trial-based partition search by default; JSON validation, CLI and packaging. Removed
unrelated algorithm dispatch, MaxRects, application SVG rendering and digest APIs.
Kept shelf/guillotine/partition-first helpers because frontier cleanup actually uses
them. Corrected legacy zero-kerf truthiness in the extracted shelf helper.

The application uses duration-dependent fallback limits (0.4-second partition
search, a dynamic layout allowance and an lb+8 target window). This package uses
explicit trial caps, optional soft time limits and searches targets up to max_sheets.
It therefore preserves the algorithm family, not bit-for-bit website behavior.

The website keeps its own application workflow. This package is an independent
deliverable; migrating the website to it is neither required nor planned. Relevant
algorithm fixes can be ported and checked in both implementations without changing
the website's integration model. This extraction does not change the license of
the parent repository, application UI or operational files.

The examples and the cases under `benchmarks/` are the designed and synthetic
cases from the application's test library. Cases derived from user accounts are
not included.

## Cut-plan correction, 2026-10-11

The original website engine and this extraction both returned no cutting steps
for `kids-desk-single-sheet`: partition-first cleanup won, but the recursive cutter
rejected edge kerfs that leave only one surviving workpiece. For example, a 703 mm
workpiece containing a 700 mm part with a 3 mm left margin needs one edge trim.
The October 11 fixed-sheet fallback had a nonempty-step guard; the normal cleanup
path and the old benchmark geometry checks did not enforce cutting completeness.

Both implementations now support kerf-consuming edge trims, retain full blade
width for sub-kerf margins, and replay-validate default sequences before returning
them. The desk case now has 19 cuts and releases all 10 parts from one sheet.
Zero-cut stock-sized parts are accepted explicitly. Tests cover all four edges,
sub-kerf margins, zero kerf, missing/reordered steps, child geometry and centerlines.

## Pattern search, 2026-10-13

A comparison with PackingSolver showed two cases where this package used one sheet
more than a proven optimum (`syn-rotation-allowed` and `garage-cabinets-imperial`).
The fixed-sheet search placed parts one at a time in a random order and never found
a split for either. `_internal/sheet_partition_search.py` now falls back to sampling
single-sheet patterns and choosing a set that covers the demand, which finds both.
The website engine received the same search on the same day. It runs under a time
budget, so it finds the `garage-cabinets-imperial` split on some runs and not others.
