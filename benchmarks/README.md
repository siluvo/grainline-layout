# Benchmark cases

Furniture cut lists for testing sheet-cutting optimizers. Each file under `cases/`
is one self-contained problem in the package's input format, with a `why` field
saying what it is meant to stress.

| Category | Meaning |
|---|---|
| `designed` | Furniture designed for this library on common sheet sizes. |
| `synthetic` | Small probes that isolate one behaviour, such as grain locks or a misleading area bound. |

## Results

Four solvers on the same cases, with the same usable sheet sizes, kerf, cut sizes
and allowed rotations. Every layout from every solver was checked for part counts,
sizes, grain, bounds, kerf spacing and guillotine separability.

- **Grainline**: this package, `balanced` preset (the default), default seed.
- **PS**: [PackingSolver](https://github.com/fontanf/packingsolver)
  `rectangleguillotine`, bin-packing objective, unlimited stages, 10 seconds per
  material. Its release of 2026-07-28, Windows build, SHA-256 of the zip
  `cc0d8fb0d2f22e0b9837ebd3ed2a3a27e787ee5818e63834232840b994b9deb5`.
- **GA** and **Greedy**: [CutStock](https://github.com/orohde/CutStock)
  `core/optimize.py`, unmodified (SHA-256
  `77962978adb43562ef52b656bbc4a6ba664d303d71e19ddf5d099dc2c99c9cd5`), its genetic
  algorithm at its defaults (population 80, 200 generations, seed 42) and its
  nested best-area-fit guillotine packer.

"Proven bound" is the lower bound on sheets that PackingSolver proved; a count
equal to it is optimal. "Largest offcut" is the area of the biggest empty
rectangle left on any sheet of that material. It only means something between
solvers that used the same number of sheets, because an extra sheet always leaves
a large empty area.

| Case | Material | Parts | Proven bound | Sheets: Grainline | PS | GA | Greedy | Largest offcut m2: Grainline | PS | GA | Greedy |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bookcase-pair-ply2500` | PLY18-2500 | 24 | 3 | 3 | 3 | 3 | 3 | 2.49 | 2.17 | 2.49 | 2.49 |
| `cabinet-set-49` | PLY18 | 30 | 5 | 5 | 5 | 6 | 6 | 0.28 | 0.35 | 1.30 | 2.51 |
| `cabinet-set-49` | MDF18 | 19 | 1 | 1 | 1 | 1 | 1 | 0.64 | 0.64 | 0.77 | 0.82 |
| `cube-storage-jp1820` | LAUAN12-1820 | 22 | 3 | 3 | 3 | 3 | 4 | 0.14 | 0.16 | 0.21 | 1.54 |
| `drawer-bank-baltic1525` | BB12-1525 | 48 | 2 | 2 | 2 | 2 | 2 | 0.61 | 0.50 | 0.24 | 0.24 |
| `drawer-bank-baltic1525` | BB6-1525 | 12 | 2 | 2 | 2 | 2 | 2 | 0.72 | 0.72 | 0.72 | 0.46 |
| `floating-shelves-strips` | PLY18 | 52 | 2 | 2 | 2 | 2 | 2 | 0.40 | 0.53 | 0.78 | 0.78 |
| `garage-cabinets-imperial` | PLY34-48x96 | 41 | 5 | 5 | 5 | 6 | 7 | 0.28 | 0.24 | 0.83 | 1.46 |
| `kids-desk-single-sheet` | PLY18 | 10 | 1 | 1 | 1 | 1 | 1 | 0.89 | 0.75 | 0.68 | 0.65 |
| `kitchen-base-ply2440-trim` | PLY18 | 29 | 3 | 4 | 4 | 4 | 4 | 2.22 | 1.17 | 1.85 | 1.83 |
| `kitchen-base-run-mfc2800` | MFC18-2800 | 48 | 3 | 3 | 3 | 3 | 4 | 1.00 | 1.98 | 0.89 | 5.42 |
| `kitchen-base-run-mfc2800` | HDF3 | 6 | 1 | 1 | 1 | 1 | 1 | 0.40 | 0.40 | 0.40 | 0.40 |
| `syn-grain-locked` | PLY18 | 24 | 4 | 5 | 5 | 5 | 5 | 1.21 | 0.79 | 0.68 | 0.53 |
| `syn-impossible-area-bound` | PLY18 | 4 | 4 | 4 | 4 | 4 | 4 | 1.39 | 1.39 | 1.39 | 1.39 |
| `syn-long-and-small` | PLY18 | 36 | 4 | 4 | 4 | 4 | 5 | 0.40 | 0.39 | 0.40 | 2.76 |
| `syn-mixed-seeded` | PLY18 | 29 | 4 | 6 | 6 | 6 | 7 | 1.05 | 1.00 | 1.07 | 2.51 |
| `syn-repeated-cabinets` | PLY18 | 36 | 5 | 6 | 6 | 6 | 6 | 0.82 | 1.22 | 1.04 | 0.73 |
| `syn-rotation-allowed` | PLY18 | 24 | 4 | 4 | 4 | 5 | 5 | 0.33 | 0.37 | 0.68 | 1.00 |
| `tv-wall-unit-ply3050` | PLY18-3050 | 24 | 3 | 3 | 3 | 3 | 3 | 2.20 | 1.66 | 1.27 | 2.23 |
| `wardrobe-tall-mfc2800` | MFC18-2800 | 35 | 4 | 4 | 4 | 4 | 4 | 2.63 | 3.48 | 2.68 | 2.96 |
| `wardrobe-tall-mfc2800` | HDF3 | 2 | 2 | 2 | 2 | 2 | 2 | 0.81 | 0.81 | 0.81 | 0.81 |
| **Total** | | 555 | 65 | **70** | 70 | 73 | 78 | | | | |

Reading it:

- Grainline and PackingSolver use the same number of sheets on every material.
  17 of the 21 are proven optimal. On the other 4 PackingSolver ran to its
  10-second limit without proving a bound or finding fewer sheets.
- Grainline used fewer sheets than the genetic algorithm on 3 materials and fewer
  than the greedy packer on 7, and never more than either.
- Two cases were tuned on. Before this comparison Grainline needed one sheet more
  on `garage-cabinets-imperial` and `syn-rotation-allowed`. A pattern search was
  added because of that. With other random seeds `garage-cabinets-imperial` still
  comes out one sheet worse about one time in eight.
- With equal sheet counts, Grainline's largest offcut was bigger than
  PackingSolver's on 8 materials, smaller on 7 and equal on 6; against the genetic
  algorithm 6, 6 and 6; against the greedy packer 6, 4 and 4. That is not a
  consistent advantage.
- Time on one desktop machine, single runs: Grainline about 34 s for all 17 cases
  (median 1.8 s per case, longest 7.1 s). PackingSolver finished 17 materials in
  under a second each and used its full 10 s on 4. Genetic algorithm about 12 s
  in total, greedy under 0.1 s. Treat these as rough.
- CutStock is a small project and its defaults were not tuned for these cases.

Grainline and PackingSolver were run on 2026-10-13 and CutStock on 2026-10-11,
with Python 3.12 on Windows.

### Reproduce

Neither solver is redistributed here. Download a PackingSolver release and
CutStock's `core/optimize.py`, then:

```sh
python -m pip install .
python benchmarks/compare_packingsolver.py --solver path/to/packingsolver_rectangleguillotine
python benchmarks/compare_cutstock.py --cutstock path/to/optimize.py
python tools/check_cases.py benchmarks/cases --baseline benchmarks/baseline.json
```

The comparison scripts validate every layout and write JSON under
`benchmarks/results/`. `check_cases.py` exits non-zero if any Grainline result is
invalid or uses more sheets than `baseline.json`.

## Comparing another solver

Solve the same case files, write results in the
[result contract](../README.md#result-contract), and run
`grainline validate <case.json> <result.json>`. When publishing a comparison,
include the options, version, hardware, elapsed time and sheet count per material.
Results that beat the table above are welcome as issues.
