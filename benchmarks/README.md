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

## Comparing another solver

Solve the same case files, write results in the
[result contract](../README.md#result-contract), and run
`grainline validate <case.json> <result.json>`. When publishing a comparison,
include the options, version, hardware, elapsed time and sheet count per material.
Results that beat the table above are welcome as issues.
