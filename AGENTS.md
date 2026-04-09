# AGENTS.md -- Schedule Generator Service

> **Caution:** The internal structure of the `api_schedule_generator` service
> may change radically between agent sessions.  File paths, module names, and
> import chains described in this document may be outdated.  Always verify the
> current directory layout and imports before relying on anything below.

## Architecture overview

The schedule generator service (`api_schedule_generator`) is a FastAPI
application that builds school timetables using a CP-SAT constraint solver.

The file **`api_schedule_generator/src/generator_v1/modules/generator_core_v4.py`** is the
**hot-swappable solver module**.  It can be replaced in-place without
modifying any other file in the service, provided the contract described
below is satisfied.

### Module graph

```text
api_schedule_generator/
  main.py (FastAPI app entry point)
  └── src/generator_v1/main.py (router, prefix=/schedule_generator/v1)
        ├── solver_loader.py          ← validates & re-exports solve_to_rows
        │     └── modules/generator_core_v4.py  ← ** HOT-SWAPPABLE SOLVER **
        ├── modules/generator.py      (data-access layer, NOT swappable)
        └── modules/teacher_timetable.py (separate module, NOT swappable)
```

At startup, `solver_loader.py` imports `generator_core_v4` and validates it
against the contract in `solver_contract.py`.  If `solve_to_rows`
is missing, has the wrong signature, or is async, the service fails immediately
with a clear error message.

---

## Hot-swap contract for `generator_core_v4.py`

### The single required function: `solve_to_rows`

This is a **synchronous** function (the router runs it in a thread pool via
`asyncio.to_thread`).  It must **not** be defined with `async def`.

#### Signature

```python
def solve_to_rows(
    input_data: dict,
    *,
    time_limit: int = 60,
    gap_weight: int = 10,
    early_weight: int = 1,
    unassigned_weight: int = 1000,
    over_capacity_strategy: str = "unassigned",
    subject_spread_strategy: str = "soft",
    subject_spread_weight: int = 5,
    log: bool = False,
) -> Tuple[Optional[List[dict]], int, Optional[float], Dict[str, Any]]
```

`input_data` is the only required positional parameter.  All others are passed
as keyword arguments by the router.  You may add extra keyword-only parameters
with defaults; the router will simply not pass them.

#### Parameters

| Name | Type | Description |
|------|------|-------------|
| `input_data` | `dict` | Full schedule input payload from the database (groups, curriculum, teachers, slots, etc.) as returned by `layout.get_auto_schedule_input_v2` |
| `time_limit` | `int` | Max solve time in seconds |
| `gap_weight` | `int` | Penalty weight for gaps between occupied slots |
| `early_weight` | `int` | Preference weight for earlier time slots |
| `unassigned_weight` | `int` | Penalty weight for unassigned lessons |
| `over_capacity_strategy` | `str` | `"unassigned"` or `"trim"` |
| `subject_spread_strategy` | `str` | `"off"`, `"soft"`, `"hard"`, or `"both"` |
| `subject_spread_weight` | `int` | Penalty weight for same-subject proximity |
| `log` | `bool` | Whether to print solver progress |

#### Return value: `(rows, status, objective, meta)`

A 4-tuple:

**`rows`** -- `Optional[List[dict]]`

On success (OPTIMAL or FEASIBLE), a flat list of schedule row dicts.  Each
dict must contain **all** of these keys:

| Key | Type | Description |
|-----|------|-------------|
| `group_id` | `int` | Class/group identifier |
| `weekday_id` | `int` | Day of the week |
| `lesson_time_id` | `int` | Time slot identifier |
| `start_time` | `str` | Lesson start time |
| `end_time` | `str` | Lesson end time |
| `subject_id` | `int` | Subject identifier |
| `teacher_id` | `int` | Teacher identifier |
| `curriculum_id` | `int` | Curriculum entry identifier |
| `subgroup_id` | `int` or `None` | Subgroup (for split classes) |
| `cabinet_id` | `int` or `None` | Room/cabinet assignment |

On failure (INFEASIBLE, MODEL_INVALID, NO_SOLUTION), must be `None`.

**`status`** -- `int`

A CP-SAT status code: `OPTIMAL` (4), `FEASIBLE` (2), `INFEASIBLE` (3),
`MODEL_INVALID` (1), `UNKNOWN` (0).  Importable from
`ortools.sat.python.cp_model`.

**`objective`** -- `Optional[float]`

The solver objective value on success, `None` on failure.

**`meta`** -- `Dict[str, Any]`

Metadata dict.  On failure it **must** contain:

| Key | Type | Description |
|-----|------|-------------|
| `infeasible_report` | `dict` | Must have at least a `"primary_cause"` key (string or `None`) |

On success it **may** contain (the router uses these if present):

| Key | Type | Description |
|-----|------|-------------|
| `rows_count` | `int` | Number of generated rows |

Any additional keys in `meta` are preserved but not read by the router.

---

## How to adapt a standalone solver to this interface

If you have a standalone solver (e.g. the workspace-root `generator.py`) that
uses a different function name or signature, add a `solve_to_rows` wrapper at
the bottom of the file.  Example skeleton:

```python
def solve_to_rows(
    input_data,
    *,
    time_limit=60,
    gap_weight=10,
    early_weight=1,
    unassigned_weight=1000,
    over_capacity_strategy="unassigned",
    subject_spread_strategy="soft",
    subject_spread_weight=5,
    log=False,
):
    # 1. Feed input_data into your model builder instead of a global variable
    # 2. Call your solver
    # 3. On success: convert your output format to flat row dicts
    # 4. On failure: build a meta dict with infeasible_report
    # 5. Return (rows_or_none, status_int, objective_or_none, meta_dict)
    ...
```

Key differences to watch for between standalone and service usage:

- The standalone solver may read from a global `data` variable.  The service
  passes input as `input_data` -- your wrapper must bridge this.
- The standalone solver may return a nested schedule dict.  The service needs
  flat row dicts (see row key table above).
- The service expects a 4-tuple return.  If your solver returns 3 values,
  your wrapper must construct the `meta` dict.
- The service handles infeasible results by inspecting `meta["infeasible_report"]`.
  Your wrapper must populate this on failure.

---

## Modification rules

### You CAN

- Change the **entire internal implementation** of the solver
  (different model, different constraints, different solver configuration).
- Add private helper functions, classes, and data structures.
- Add new public functions beyond `solve_to_rows` (they will be ignored).
- Add extra keyword-only parameters with defaults to `solve_to_rows`.
- Import any standard-library or third-party package.

### You MUST NOT

- **Remove or rename** `solve_to_rows`.
- **Remove the `input_data` positional parameter** or change it to keyword-only.
- Make `solve_to_rows` an **async** function -- the router calls it via
  `asyncio.to_thread`, which requires a sync callable.
- Change the **return type** from a 4-tuple to something else.
- Remove required keys from the row dicts or the failure `meta` dict.

### You SHOULD

- Keep the file at `api_schedule_generator/src/generator_v1/modules/generator_core_v4.py`.
  The import path is hard-coded in `solver_loader.py`.
- Run the service after swapping the file to verify the startup validation
  passes.  If there is a contract violation you will see an `ImportError` with
  a specific list of what is wrong.

---

## Files you should NOT touch when modifying the solver

| File | Reason |
|------|--------|
| `api_schedule_generator/src/generator_v1/main.py` | The router -- defines API endpoints and the background job runner |
| `api_schedule_generator/src/generator_v1/solver_loader.py` | Validates the solver against the contract |
| `api_schedule_generator/src/generator_v1/solver_contract.py` | Defines the contract itself |
| `api_schedule_generator/src/generator_v1/modules/generator.py` | Data-access layer (DB reads/writes) -- separate concern |
| `api_schedule_generator/src/generator_v1/modules/teacher_timetable.py` | Teacher timetable CRUD -- separate concern |
| `api_schedule_generator/src/generator_v1/models.py` | Pydantic models shared across modules |
| `api_schedule_generator/src/generator_v1/serializers.py` | Key-case converters shared across modules |
| `api_schedule_generator/lib/*` | Shared service infrastructure |
| `api_schedule_generator/main.py` | FastAPI app factory |

---

## How startup validation works

When the service starts, Python imports the router (`src/generator_v1/main.py`),
which imports `solver_loader.py`.  The loader:

1. Imports `generator_core_v4` from `src.generator_v1.modules`.
2. Checks that `solve_to_rows` exists and is callable.
3. Verifies it is **not** an async function.
4. Verifies it has at least 1 positional parameter (`input_data`).
5. If any check fails, the service raises `ImportError` with a detailed message.

This catches a broken solver file **before the first HTTP request**, not at
runtime in the middle of a schedule generation job.
