# Grainline Layout

Grain-aware sheet cutting layouts for plywood, MDF and other panel stock: give it
a cut list, get back which part goes on which sheet, plus a step-by-step
guillotine cutting sequence you can follow on a table saw or panel saw. Python API
and a local JSON CLI.

This is the layout engine behind [Grainline](https://siluvo.com), the cut-list
optimizer by Siluvo LLC. The website adds the project editor, cutting animation
and printable plans; this package is the solver on its own.

- **Grain direction is a constraint, not an afterthought.** Parts are only rotated
  when their grain setting allows it.
- **Every layout is guillotine-cuttable**, and the cutting sequence is replayed
  and checked before it is returned.
- **Independent validator included.** `grainline validate` checks any result
  against its problem, so you can verify layouts from this or another solver.
- **No dependencies.** Python 3.10+ standard library only: no account, database,
  configuration or network access.
- **Published test cases.** See [`benchmarks/`](benchmarks/README.md) for the
  furniture cut lists and the sheet counts this version reaches on them.

Licensed under Apache-2.0; see `LICENSE` and `NOTICE`.

## Install

```sh
python -m pip install git+https://github.com/siluvo/grainline-layout
grainline --version
```

From a checkout, use `pip install .` (or `pip install -e .` for development).
Packaging needs setuptools; runtime has no third-party dependencies. The Python
distribution is named `grainline-core` and the import name is `grainline`.

## CLI

```sh
grainline optimize examples/cabinet-set-49.json -o result.json
grainline optimize examples/cabinet-set-49.json --preset thorough --random-seed 42 -o thorough.json
grainline optimize examples/cabinet-set-49.json --partition-tries 0 -o frontier-only.json
grainline optimize examples/cabinet-set-49.json --partition-tries 12000 --partition-plans 6 -o custom.json
grainline optimize examples/cabinet-set-49.json --time-limit 10 -o bounded.json
grainline validate examples/cabinet-set-49.json result.json
```

`python -m grainline` is equivalent to the console script. `-` selects stdin or
stdout. JSON output is UTF-8; diagnostics go to stderr. An output file is replaced
atomically, and cannot be the input file. Exit codes: **0** complete / validation
passed; **2** invalid input or I/O error; **3** incomplete / validation failed;
**4** internal solver validity failure. An incomplete solve still writes its result.

## Python

```python
import json
from grainline import SearchOptions, optimize, validate

with open("examples/cabinet-set-49.json", encoding="utf-8") as stream:
    problem = json.load(stream)

options = SearchOptions.from_preset("balanced", partition_tries=12000, random_seed=42)
result = optimize(problem, options=options)
assert not validate(problem, result)
print(result["status"], result["sheets_used"], result["search"])
```

Passing `options=` replaces all embedded options. Without it, `problem.options`
may contain `preset` and any SearchOptions field. In the CLI, explicit flags override
the corresponding embedded options; presets fill defaults, then individual values
override them. Unknown search options are rejected.

## Search budgets, not different algorithms

All presets use the same adaptive-frontier engine plus optional fixed-sheet
partition search. They are **not** a greedy / genetic algorithm switch.

| Setting / CLI flag | fast | balanced (default) | thorough |
|---|---:|---:|---:|
| `seed_limit` / `--seed-limit` | 2 | 5 | 10 |
| `candidate_pool_limit` / `--candidate-pool-limit` | 8 | 18 | 36 |
| `partition_tries` / `--partition-tries` | 500 | 6000 | 24000 |
| `partition_plans` / `--partition-plans` | 1 | 3 | 8 |
| `seed_early_stop` / `--[no-]seed-early-stop` | true | true | false |

- **Starting layouts:** `seed_limit` is the number of candidate starting *pieces*,
  not the random seed or exact number of layouts. An automatic start plus allowed
  orientations can create more trials. Duplicate layouts can end this search early.
- **Candidate pool:** limits which pieces compete during placement. Minimum 8.
- **Partition trials:** randomized search for how to divide parts across a smaller
  fixed number of sheets. Maximum trials **per target sheet count per material**;
  stops earlier when enough distinct plans are found. Set 0 to disable this fallback.
- **Partition plans:** maximum distinct splits considered per target. The first
  valid plan reaching that target is accepted; the budget is a cap, not required work.
- **Random seed:** `random_seed` / `--random-seed` defaults to 20261011, affects only
  randomized partition search. The deterministic frontier search does not use it.
- **Sheet cap:** `max_sheets` / `--max-sheets` defaults to 100 **per material group**.
- **Time caps:** `time_limit` / `--time-limit` is a soft total solve budget;
  `partition_time_limit` / `--partition-time-limit` is a soft fallback budget per
  material. Both default to `None`. Existing atomic layout / cleanup / validation
  work can overrun a cap. Each material gets at least one attempt so the result
  accounts for every part, even when the budget has expired. This is not a hard
  process timeout. Results may be incomplete; inspect `status` and `search`.

The default uses fixed trials, no wall-clock stopping. Identical ordered inputs,
options and package versions are intended to reproduce placements/cutting data
on the same Python/runtime; timing telemetry naturally differs. Time-limited runs
depend on machine load. Larger budgets and `thorough` can choose different search
paths and **do not guarantee fewer sheets or a globally optimal result**. The
current partition fallback skips targets requiring over 93% utilization. Neither
budget exhaustion nor failure to find a layout proves infeasibility.

For fair comparisons, publish the full options, version, input hash, hardware,
elapsed time, validity, sheet count **per material**, utilization and cutting-plan
availability. Compare fixed-budget runs separately from time-limited runs. Actual
seed trials and partition target statistics are included in `telemetry`.

## Input contract (schema version 1)

```json
{
  "units": "mm",
  "kerf_mm": 3,
  "boards": [
    {"code": "PLY18", "length_mm": 2440, "width_mm": 1220,
     "thickness_mm": 18, "grain_axis": "long",
     "trim_mm": {"left": 0, "right": 0, "top": 0, "bottom": 0}}
  ],
  "parts": [
    {"id": "side", "name": "Cabinet side", "board": "PLY18",
     "length_mm": 1800, "width_mm": 550, "quantity": 2,
     "grain_direction": "length"}
  ],
  "options": {"preset": "balanced", "random_seed": 42}
}
```

- Units are millimetres only. Dimensions must be finite and positive; kerf and
  trims may be zero. Up to 500 expanded parts per solve. Quantity is a positive integer.
- Boards are unlimited identical stock within each material's sheet cap; finite
  inventory and substitution between board codes are not supported.
- `grain_axis`: `long`, `short`, `none`, relative to the board's geometric edges.
  Part `grain_direction`: `length`, `width`, `any`. Rotation follows these constraints.
  Legacy direction aliases (including `long`, `short`, `none` and null) are normalized;
  unrecognized directions are rejected rather than silently dropping the constraint.
- `code` identifies a board/material group; `board` on each part must reference it.
  Optional part IDs must be unique and contain no `:`; defaults are `p0`, `p1`, ... .
- Optional `cut_length_mm` and `cut_width_mm` must be supplied together and must
  not exceed finished dimensions. These are the dimensions physically laid out,
  after edge-band deductions. The API does not resolve a material database.
- `name`, `id`, `title`, `category`, `why` and other descriptive case metadata do
  not affect layout. Canonical operational field names above are required.

## Result contract

Top-level `status`, `sheets_used`, `unplaced_count`, `validation`, `search`, and
`boards` summarize the run. Each board group has `board_id`, `required_sheets`,
`usable_length_mm`, `usable_width_mm` and `layouts`. Layouts include `placements`,
`leftovers`, `cut_plan_steps`, `cut_plan_modes` and diagnostics. Coordinates use
the usable area's local origin (board trim is excluded): x along input board length,
y along input board width. Placement IDs include an occurrence suffix.

`complete` means all requested parts were placed in geometrically validated,
guillotine-cuttable layouts. It does **not** mean optimal or machine-ready.
Default cut sequences are replay-validated: each cut consumes a live workpiece,
preserves finished parts, and ends with every part isolated. Edge trimming can
legitimately leave one surviving workpiece. An exact stock-sized part requires
zero cuts; `cut_plan_meta.validation.cut_required` reports that case explicitly.
Missing or incomplete required sequences are rejected, not accepted with a warning.
The independent validator checks multiplicity, material, dimensions, rotation/grain,
sheet bounds, kerf separation, recursive guillotine geometry and the default
through-cut replay. It does not certify machine operations or cutting safety. Treat diagnostic details as
unstable while the package is at version 0.1.

## Development checks

```sh
python -m unittest discover -s tests -v
python tools/check_cases.py benchmarks/cases --baseline benchmarks/baseline.json
```

`tools/check_cases.py` solves every case, validates the result and fails when a
material needs more sheets than the recorded baseline. See `PROVENANCE.md` for how
this package relates to the engine running on the website.
