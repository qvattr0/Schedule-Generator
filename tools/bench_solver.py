#!/usr/bin/env python3
import argparse
import itertools
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generator  # noqa: E402
from ortools.sat.python import cp_model  # noqa: E402
from render_schedule import render_schedule  # noqa: E402


STATUS_NAMES: dict[cp_model.CpSolverStatus, str] = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.UNKNOWN: "UNKNOWN",
}


class _CpModelWithStats(Protocol):
    def ModelStats(self) -> str: ...


def status_name(status: cp_model.CpSolverStatus) -> str:
    return STATUS_NAMES.get(status, str(status))


def timed_call(fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = time.perf_counter() - started
    return result, elapsed


def model_stats_text(model: cp_model.CpModel) -> str:
    return cast(_CpModelWithStats, model).ModelStats()


def parse_int_csv(raw: Optional[str]) -> list[int]:
    if raw is None or not raw.strip():
        return []
    values: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(int(item))
    return values


def parse_symmetry_csv(raw: Optional[str]) -> list[Optional[int]]:
    if raw is None or not raw.strip():
        return []
    values: list[Optional[int]] = []
    for part in raw.split(","):
        item = part.strip().lower()
        if not item:
            continue
        if item == "auto":
            values.append(None)
        else:
            values.append(int(item))
    return values


def parse_presolve_csv(raw: Optional[str]) -> list[str]:
    if raw is None or not raw.strip():
        return []
    values: list[str] = []
    for part in raw.split(","):
        item = part.strip().lower()
        if not item:
            continue
        if item not in generator.CP_MODEL_PRESOLVE_CHOICES:
            raise ValueError(
                f"Invalid presolve value '{item}'. "
                f"Expected one of: {', '.join(generator.CP_MODEL_PRESOLVE_CHOICES)}"
            )
        values.append(item)
    return values


def summarize_model_stats(model: cp_model.CpModel, lines: int = 12) -> str:
    stats_lines = model_stats_text(model).splitlines()
    return "\n".join(stats_lines[: max(1, lines)])


def solver_stats_dict(
    solver: cp_model.CpSolver, status: cp_model.CpSolverStatus
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "status": status_name(status),
        "solver_wall_s": round(float(solver.WallTime()), 3),
        "branches": int(solver.NumBranches()),
        "conflicts": int(solver.NumConflicts()),
    }
    try:
        stats["best_bound"] = round(float(solver.BestObjectiveBound()), 3)
    except Exception:
        pass
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        try:
            stats["objective"] = round(float(solver.ObjectiveValue()), 3)
        except Exception:
            pass
    return stats


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark OR-Tools schedule solver phases and solver parameter sweeps"
    )
    parser.add_argument("--time-limit", type=int, default=8, help="Max solve time in seconds")
    parser.add_argument(
        "--solver-profile",
        choices=generator.SOLVER_PROFILE_CHOICES,
        default="default",
        help="Solver parameter profile to use for the baseline run.",
    )
    parser.add_argument("--num-search-workers", type=int, default=None)
    parser.add_argument("--symmetry-level", type=int, default=None)
    parser.add_argument(
        "--cp-model-presolve",
        choices=generator.CP_MODEL_PRESOLVE_CHOICES,
        default="auto",
    )
    parser.add_argument("--random-seed", type=int, default=None)
    parser.add_argument(
        "--sweep-workers",
        default=None,
        help="Comma-separated worker counts (e.g. 1,4,8,16)",
    )
    parser.add_argument(
        "--sweep-symmetry",
        default=None,
        help="Comma-separated symmetry levels or 'auto' (e.g. auto,0,1)",
    )
    parser.add_argument(
        "--sweep-presolve",
        default=None,
        help="Comma-separated cp_model_presolve values (auto,on,off)",
    )
    parser.add_argument(
        "--full-model-stats",
        action="store_true",
        help="Print full ModelStats() output instead of a short summary.",
    )
    parser.add_argument(
        "--extract-schedule",
        action="store_true",
        help="Time schedule extraction when a feasible solution is found.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON output path for extracted schedule (requires --extract-schedule).",
    )
    parser.add_argument(
        "--render",
        nargs="?",
        const="schedule.html",
        default=None,
        help="Render HTML timetable after extraction and write to this path.",
    )
    parser.add_argument("--render-group", type=int, default=None)
    parser.add_argument(
        "--log",
        action="store_true",
        help="Enable CP-SAT search progress logs for the baseline run.",
    )

    parser.add_argument(
        "--gap-weight",
        type=int,
        default=None,
        help="Override gap objective weight.",
    )
    parser.add_argument(
        "--early-weight",
        type=int,
        default=None,
        help="Override early-slot objective weight.",
    )
    parser.add_argument(
        "--daily-balance-weight",
        type=int,
        default=None,
        help="Override daily-balance objective weight.",
    )
    parser.add_argument(
        "--unassigned-weight",
        type=int,
        default=None,
        help="Override unassigned objective weight.",
    )
    parser.add_argument(
        "--over-capacity-strategy",
        choices=["unassigned", "trim"],
        default="unassigned",
    )
    parser.add_argument(
        "--subject-spread-strategy",
        choices=["off", "soft", "hard", "both"],
        default="soft",
    )
    parser.add_argument(
        "--subject-spread-weight",
        type=int,
        default=None,
        help="Override subject-spread soft objective weight.",
    )
    parser.add_argument("--ignore-availability", action="store_true")
    return parser


def solve_once(
    model: cp_model.CpModel,
    *,
    time_limit: int,
    solver_profile: str,
    num_search_workers: Optional[int],
    symmetry_level: Optional[int],
    cp_model_presolve: str,
    random_seed: Optional[int],
    log: bool = False,
) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus, float, dict[str, Any]]:
    solver = cp_model.CpSolver()
    applied = generator.configure_solver(
        solver,
        time_limit=time_limit,
        log=log,
        solver_profile=solver_profile,
        num_search_workers=num_search_workers,
        symmetry_level=symmetry_level,
        cp_model_presolve=cp_model_presolve,
        random_seed=random_seed,
    )
    started = time.perf_counter()
    status = solver.Solve(model)
    elapsed = time.perf_counter() - started
    return solver, status, elapsed, applied


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    generator.apply_objective_weight_precedence(args)

    phase_times: dict[str, float] = {}

    validation_report, phase_times["validate_s"] = timed_call(
        generator.validate_teacher_week_count_sum_consistency, generator.data
    )
    if validation_report.get("mismatch_count", 0) > 0:
        raise generator.DataValidationError(
            generator.format_teacher_week_count_sum_validation_error(validation_report)
        )

    _, phase_times["analyze_infeasibility_s"] = timed_call(
        generator.analyze_infeasibility,
        generator.data,
        over_capacity_strategy=args.over_capacity_strategy,
        subject_spread_strategy=args.subject_spread_strategy,
        ignore_availability=args.ignore_availability,
    )

    model_parts, phase_times["build_model_s"] = timed_call(
        generator.build_model,
        gap_weight=args.gap_weight,
        early_weight=args.early_weight,
        daily_balance_weight=args.daily_balance_weight,
        unassigned_weight=args.unassigned_weight,
        over_capacity_strategy=args.over_capacity_strategy,
        subject_spread_strategy=args.subject_spread_strategy,
        subject_spread_weight=args.subject_spread_weight,
        ignore_availability=args.ignore_availability,
        diagnose_unsat=False,
    )
    model, x, teacher_choice, _occ, group_info, _assumptions = model_parts

    print("[phase-times]")
    for key, value in phase_times.items():
        print(f"{key}: {value:.3f}s")

    print()
    print("[model-stats]")
    if args.full_model_stats:
        print(model_stats_text(model))
    else:
        print(summarize_model_stats(model))

    print()
    print("[baseline-solve]")
    solver, status, solve_elapsed, applied = solve_once(
        model,
        time_limit=args.time_limit,
        solver_profile=args.solver_profile,
        num_search_workers=args.num_search_workers,
        symmetry_level=args.symmetry_level,
        cp_model_presolve=args.cp_model_presolve,
        random_seed=args.random_seed,
        log=args.log,
    )
    print(f"applied_parameters: {json.dumps(applied, sort_keys=True)}")
    print(f"wall_s: {solve_elapsed:.3f}")
    print(json.dumps(solver_stats_dict(solver, status), sort_keys=True))

    if args.extract_schedule and status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        schedule, extract_s = timed_call(
            generator.extract_schedule_from_solution, solver, x, teacher_choice, group_info
        )
        print()
        print("[post-solve]")
        print(f"extract_schedule_s: {extract_s:.3f}s")

        if args.output:
            output_path = Path(args.output)
            started = time.perf_counter()
            output_path.write_text(json.dumps(schedule, indent=2), encoding="utf-8")
            write_s = time.perf_counter() - started
            print(f"write_json_s: {write_s:.3f}s")
            print(f"wrote_json: {output_path}")

        if args.render:
            started = time.perf_counter()
            html = render_schedule(schedule, group_id=args.render_group)
            render_cpu_s = time.perf_counter() - started
            render_path = Path(args.render)
            started = time.perf_counter()
            render_path.write_text(html, encoding="utf-8")
            render_write_s = time.perf_counter() - started
            print(f"render_html_s: {render_cpu_s:.3f}s")
            print(f"write_html_s: {render_write_s:.3f}s")
            print(f"wrote_html: {render_path}")

    sweep_workers = parse_int_csv(args.sweep_workers)
    sweep_symmetry = parse_symmetry_csv(args.sweep_symmetry)
    sweep_presolve = parse_presolve_csv(args.sweep_presolve)

    if sweep_workers or sweep_symmetry or sweep_presolve:
        workers_vals: Iterable[Optional[int]] = (
            sweep_workers if sweep_workers else [args.num_search_workers]
        )
        symmetry_vals: Iterable[Optional[int]] = (
            sweep_symmetry if sweep_symmetry else [args.symmetry_level]
        )
        presolve_vals: Iterable[str] = (
            sweep_presolve if sweep_presolve else [args.cp_model_presolve]
        )

        print()
        print("[solver-sweep]")
        for workers, symmetry, presolve in itertools.product(
            workers_vals, symmetry_vals, presolve_vals
        ):
            run_solver, run_status, run_wall_s, run_applied = solve_once(
                model,
                time_limit=args.time_limit,
                solver_profile=args.solver_profile,
                num_search_workers=workers,
                symmetry_level=symmetry,
                cp_model_presolve=presolve,
                random_seed=args.random_seed,
                log=False,
            )
            row = {
                "workers": workers,
                "symmetry_level": symmetry,
                "cp_model_presolve": presolve,
                "applied": run_applied,
                "wall_s": round(run_wall_s, 3),
            }
            row.update(solver_stats_dict(run_solver, run_status))
            print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
