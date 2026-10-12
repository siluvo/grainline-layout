# Benchmark cases

Furniture cut lists for testing sheet-cutting optimizers. Each file under `cases/`
is one self-contained problem in the package's input format, with a `why` field
saying what it is meant to stress.

| Category | Meaning |
|---|---|
| `designed` | Furniture designed for this library on common sheet sizes. |
| `synthetic` | Small probes that isolate one behaviour, such as grain locks or a misleading area bound. |

## Results for this version

`balanced` preset (the default), fixed trial budgets, default random seed.
Every result below passed the independent validator: all parts placed, grain
respected, parts at least one kerf apart, every sheet guillotine-cuttable, and
the cutting sequence replayed to completion.

| Case | Material | Parts | Area lower bound | Grainline sheets | At lower bound |
|---|---|---:|---:|---:|:---:|
| `bookcase-pair-ply2500` | PLY18-2500 | 24 | 2 | 3 |  |
| `cabinet-set-49` | PLY18 | 30 | 5 | 5 | yes |
| `cabinet-set-49` | MDF18 | 19 | 1 | 1 | yes |
| `cube-storage-jp1820` | LAUAN12-1820 | 22 | 3 | 3 | yes |
| `drawer-bank-baltic1525` | BB12-1525 | 48 | 2 | 2 | yes |
| `drawer-bank-baltic1525` | BB6-1525 | 12 | 2 | 2 | yes |
| `floating-shelves-strips` | PLY18 | 52 | 2 | 2 | yes |
| `garage-cabinets-imperial` | PLY34-48x96 | 41 | 5 | 6 |  |
| `kids-desk-single-sheet` | PLY18 | 10 | 1 | 1 | yes |
| `kitchen-base-ply2440-trim` | PLY18 | 29 | 3 | 4 |  |
| `kitchen-base-run-mfc2800` | MFC18-2800 | 48 | 3 | 3 | yes |
| `kitchen-base-run-mfc2800` | HDF3 | 6 | 1 | 1 | yes |
| `syn-grain-locked` | PLY18 | 24 | 4 | 5 |  |
| `syn-impossible-area-bound` | PLY18 | 4 | 2 | 4 |  |
| `syn-long-and-small` | PLY18 | 36 | 4 | 4 | yes |
| `syn-mixed-seeded` | PLY18 | 29 | 4 | 6 |  |
| `syn-repeated-cabinets` | PLY18 | 36 | 5 | 6 |  |
| `syn-rotation-allowed` | PLY18 | 24 | 4 | 5 |  |
| `tv-wall-unit-ply3050` | PLY18-3050 | 24 | 3 | 3 | yes |
| `wardrobe-tall-mfc2800` | MFC18-2800 | 35 | 3 | 4 |  |
| `wardrobe-tall-mfc2800` | HDF3 | 2 | 2 | 2 | yes |

12 of 21 material groups reach the area lower bound. The lower bound is
total part area divided by usable sheet area, rounded up. It ignores shapes, kerf
and grain, so it is often unreachable: a count above it does not show that a
better layout exists, while a count equal to it is optimal. The solver is a
heuristic and does not prove optimality.

## Reproduce

```sh
python -m pip install .
python tools/check_cases.py benchmarks/cases --baseline benchmarks/baseline.json
```

The checker prints one JSON line per case with sheet counts and elapsed seconds,
and exits non-zero if any result is invalid or uses more sheets than
`baseline.json`.

## Compared with a greedy packer and a genetic algorithm

[CutStock](https://github.com/orohde/CutStock) implements the two usual approaches
in plain Python with kerf and grain support: a nested best-area-fit guillotine
packer and a genetic algorithm over part order. We ran its `core/optimize.py`
unmodified (SHA-256 `77962978adb43562ef52b656bbc4a6ba664d303d71e19ddf5d099dc2c99c9cd5`)
at its defaults (population 80, 200 generations, seed 42) on the same cases, with
the same usable sheet sizes, kerf, cut sizes and allowed rotations. Every layout
from all three was checked for part counts, sizes, grain, bounds, kerf spacing
and guillotine separability.

"Largest offcut" is the area of the biggest empty rectangle left on any sheet of
that material. It only means something between methods that used the same number
of sheets, because an extra sheet always leaves a large empty area.

| Case | Material | Parts | Lower bound | Sheets: Grainline | GA | Greedy | Largest offcut m2: Grainline | GA | Greedy |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `bookcase-pair-ply2500` | PLY18-2500 | 24 | 2 | 3 | 3 | 3 | 2.49 | 2.49 | 2.49 |
| `cabinet-set-49` | PLY18 | 30 | 5 | 5 | 6 | 6 | 0.28 | 1.30 | 2.51 |
| `cabinet-set-49` | MDF18 | 19 | 1 | 1 | 1 | 1 | 0.64 | 0.77 | 0.82 |
| `cube-storage-jp1820` | LAUAN12-1820 | 22 | 3 | 3 | 3 | 4 | 0.14 | 0.21 | 1.54 |
| `drawer-bank-baltic1525` | BB12-1525 | 48 | 2 | 2 | 2 | 2 | 0.61 | 0.24 | 0.24 |
| `drawer-bank-baltic1525` | BB6-1525 | 12 | 2 | 2 | 2 | 2 | 0.72 | 0.72 | 0.46 |
| `floating-shelves-strips` | PLY18 | 52 | 2 | 2 | 2 | 2 | 0.40 | 0.78 | 0.78 |
| `garage-cabinets-imperial` | PLY34-48x96 | 41 | 5 | 6 | 6 | 7 | 0.92 | 0.83 | 1.46 |
| `kids-desk-single-sheet` | PLY18 | 10 | 1 | 1 | 1 | 1 | 0.89 | 0.68 | 0.65 |
| `kitchen-base-ply2440-trim` | PLY18 | 29 | 3 | 4 | 4 | 4 | 2.22 | 1.85 | 1.83 |
| `kitchen-base-run-mfc2800` | MFC18-2800 | 48 | 3 | 3 | 3 | 4 | 1.00 | 0.89 | 5.42 |
| `kitchen-base-run-mfc2800` | HDF3 | 6 | 1 | 1 | 1 | 1 | 0.40 | 0.40 | 0.40 |
| `syn-grain-locked` | PLY18 | 24 | 4 | 5 | 5 | 5 | 1.21 | 0.68 | 0.53 |
| `syn-impossible-area-bound` | PLY18 | 4 | 2 | 4 | 4 | 4 | 1.39 | 1.39 | 1.39 |
| `syn-long-and-small` | PLY18 | 36 | 4 | 4 | 4 | 5 | 0.40 | 0.40 | 2.76 |
| `syn-mixed-seeded` | PLY18 | 29 | 4 | 6 | 6 | 7 | 1.05 | 1.07 | 2.51 |
| `syn-repeated-cabinets` | PLY18 | 36 | 5 | 6 | 6 | 6 | 0.82 | 1.04 | 0.73 |
| `syn-rotation-allowed` | PLY18 | 24 | 4 | 5 | 5 | 5 | 0.77 | 0.68 | 1.00 |
| `tv-wall-unit-ply3050` | PLY18-3050 | 24 | 3 | 3 | 3 | 3 | 2.20 | 1.27 | 2.23 |
| `wardrobe-tall-mfc2800` | MFC18-2800 | 35 | 3 | 4 | 4 | 4 | 2.63 | 2.68 | 2.96 |
| `wardrobe-tall-mfc2800` | HDF3 | 2 | 2 | 2 | 2 | 2 | 0.81 | 0.81 | 0.81 |
| **Total** | | 555 | 61 | **72** | 73 | 78 | | | |

Reading it:

- Grainline never used more sheets than either method. It used fewer than the
  greedy packer on 6 materials and fewer than the genetic algorithm on 1
  (`cabinet-set-49` plywood).
- With equal sheet counts, Grainline's largest offcut was bigger than the genetic
  algorithm's on 8 materials, smaller on 6 and equal on 6; against the greedy
  packer, bigger on 6, smaller on 5 and equal on 4. That is not a consistent
  advantage.
- Solve time for all 17 cases on one desktop machine, single runs: Grainline about
  30 s (median 1.2 s per case, longest 6.9 s), genetic algorithm about 12 s,
  greedy under 0.1 s. Treat these as rough; they were not measured under
  controlled conditions.
- CutStock is a small project and its defaults were not tuned for these cases. A
  longer genetic run could close the one-sheet gap.

CutStock layouts were produced on 2026-10-11 and Grainline layouts on 2026-10-13,
with Python 3.12 on Windows.

### Reproduce the comparison

CutStock is not redistributed here. Download its `core/optimize.py`, then:

```sh
python benchmarks/compare_cutstock.py --cutstock path/to/optimize.py
```

The script checks the file's SHA-256, runs all three solvers, validates every
layout and writes `benchmarks/results/cutstock.json`.

## Comparing another solver

Solve the same case files, write results in the
[result contract](../README.md#result-contract), and run
`grainline validate <case.json> <result.json>`. When publishing a comparison,
include the options, version, hardware, elapsed time and sheet count per material.
Results that beat the table above are welcome as issues.
