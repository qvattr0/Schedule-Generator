# Schedule Generator

This project generates weekly group timetables from the dataset defined in `mock_data.py` using the OR-Tools CP-SAT solver.

The solver builds a schedule that:

- assigns the required weekly lessons for each curriculum bundle
- prevents teacher double-booking across groups
- respects teacher busy slots from `teachers_busy` unless told to ignore them
- respects each group's `max_lessons_per_day`
- supports linked curricula via `match_with_curriculum_id`
- optimizes the result with soft penalties for gaps, early slots, uneven daily load, unassigned overflow lessons, and subject clustering

The main output is a JSON schedule file. It can also render that schedule into an HTML timetable.

## Project Files

- `generator.py`: main CLI entry point that validates data, builds the CP-SAT model, solves it, and writes `schedule.json`
- `render_schedule.py`: converts a generated schedule JSON file into `schedule.html`
- `mock_data.py`: in-repo input dataset used by the generator
- `objective_weights.toml`: default soft-objective weights
- `tools/bench_solver.py`: helper for timing the solver and sweeping solver parameters

## Requirements

- Python 3.11+
- OR-Tools for Python

Install OR-Tools:

```bash
python3 -m pip install ortools
```

Python 3.11+ is recommended because `objective_weights.toml` is loaded with `tomllib`.

## Quick Start

Generate a schedule JSON file:

```bash
python3 generator.py
```

Generate a schedule and render HTML:

```bash
python3 generator.py --render
```

Generate a schedule, write custom output paths, and open the HTML file:

```bash
python3 generator.py --output custom_schedule.json --render custom_schedule.html --open
```

Render an existing JSON schedule separately:

```bash
python3 render_schedule.py --input schedule.json --output schedule.html
```

## Important Input/Config Notes

- `generator.py` does not currently accept an input file path. It always reads data from `mock_data.py`.
- If you want to change the scheduling input, edit `mock_data.py`.
- Default objective weights come from `objective_weights.toml`.
- CLI weight flags override values from `objective_weights.toml`.
- Before solving, the generator validates teacher weekly load totals. If the declared teacher caps do not match the aggregated curriculum demand, the run stops with an error.

## `generator.py` Usage

Basic form:

```bash
python3 generator.py [options]
```

### Solver and runtime options

- `--time-limit SECONDS`
  Maximum solve time. Default: `60`
- `--solver-profile {default,first-feasible}`
  Solver parameter preset. `first-feasible` favors getting a schedule sooner.
- `--num-search-workers N`
  Override CP-SAT `num_search_workers`
- `--symmetry-level N`
  Override CP-SAT `symmetry_level`
- `--cp-model-presolve {auto,on,off}`
  Control CP-SAT presolve behavior
- `--random-seed N`
  Set a repeatable solver seed
- `--log`
  Enable CP-SAT search progress logs

### Schedule behavior options

- `--over-capacity-strategy {unassigned,trim}`
  Controls what happens when a group needs more lessons than it has capacity for
  - `unassigned`: keep the full demand and penalize missing lessons
  - `trim`: trim lesson counts down to fit capacity
- `--subject-spread-strategy {off,soft,hard,both}`
  Controls how repeated subjects are spread across the week
- `--ignore-availability`
  Ignore `teachers_busy` constraints

### Objective weight options

If these are omitted, the values come from `objective_weights.toml` and then fall back to built-in defaults.

- `--gap-weight N`
  Penalize empty gaps inside a day's occupied block
- `--early-weight N`
  Penalize earlier lesson positions
- `--daily-balance-weight N`
  Penalize uneven day-to-day lesson counts
- `--unassigned-weight N`
  Penalize unscheduled lessons when over capacity
- `--subject-spread-weight N`
  Penalize same-subject lessons being too close together when soft spread is active

### Diagnostics options

- `--diagnose-unsat`
  Print an assumption-based unsat core summary when the model is infeasible
- `--unsat-core-max-items N`
  Limit how many individual unsat-core items are printed. Default: `20`

### Output options

- `--output PATH`
  JSON output path. Default: `schedule.json`
- `--render [PATH]`
  Render HTML after solving. If no path is given, the default is `schedule.html`
- `--render-group GROUP_ID`
  Render only one group when `--render` is used
- `--open`
  Open the rendered HTML in the default browser. Requires `--render`

### Examples

Run with defaults:

```bash
python3 generator.py
```

Favor a quick first feasible result:

```bash
python3 generator.py --solver-profile first-feasible --time-limit 20
```

Render only one group:

```bash
python3 generator.py --render schedule.html --render-group 101
```

Tune objective weights from the terminal instead of the TOML file:

```bash
python3 generator.py --gap-weight 20 --early-weight 0 --daily-balance-weight 8
```

Diagnose an infeasible model:

```bash
python3 generator.py --diagnose-unsat --unsat-core-max-items 50
```

Ignore teacher busy slots:

```bash
python3 generator.py --ignore-availability
```

## `render_schedule.py` Usage

Basic form:

```bash
python3 render_schedule.py --input schedule.json --output schedule.html
```

Options:

- `--input PATH`
  Input schedule JSON file. Default: `schedule.json`
- `--output PATH`
  Output HTML file. Default: `schedule.html`
- `--group GROUP_ID`
  Render only one group

Example:

```bash
python3 render_schedule.py --input custom_schedule.json --output custom_schedule.html --group 101
```

## `tools/bench_solver.py` Usage

This helper is useful when you want to measure model build time, solve time, and compare solver parameter combinations.

Basic form:

```bash
python3 tools/bench_solver.py [options]
```

Common options:

- `--time-limit SECONDS`
- `--solver-profile {default,first-feasible}`
- `--num-search-workers N`
- `--symmetry-level N`
- `--cp-model-presolve {auto,on,off}`
- `--random-seed N`
- `--full-model-stats`
- `--extract-schedule`
- `--output PATH`
- `--render [PATH]`
- `--render-group GROUP_ID`
- `--sweep-workers CSV`
- `--sweep-symmetry CSV`
- `--sweep-presolve CSV`
- `--log`

It also accepts the same scheduling and objective-weight flags as `generator.py`.

Example:

```bash
python3 tools/bench_solver.py \
  --time-limit 8 \
  --extract-schedule \
  --output bench_schedule.json \
  --render bench_schedule.html \
  --sweep-workers 1,4,8 \
  --sweep-presolve auto,on,off
```

## Typical Workflow

1. Edit the source dataset in `mock_data.py`.
2. Adjust default weights in `objective_weights.toml` if needed.
3. Run `python3 generator.py --render`.
4. Inspect `schedule.json` and `schedule.html`.
5. Use `tools/bench_solver.py` if you want to compare solver settings or performance.
