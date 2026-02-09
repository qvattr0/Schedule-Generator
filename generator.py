import argparse
import collections
import json
import sys
import webbrowser
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple
from ortools.sat.python import cp_model
from mock_data import data_v2 as data
from render_schedule import render_schedule


@dataclass(frozen=True)
class Bundle:
    id: int
    curriculum_ids: List[int]
    teacher_ids: List[int]
    subject_ids: List[int]
    lesson_count: int


@dataclass(frozen=True)
class Slot:
    id: int
    day_index: int
    pos: int
    weekday_id: int
    weekday_name: str
    lesson_time_id: int
    start_time: str
    end_time: str


def _build_bundles(curriculum_data: List[dict]) -> Tuple[List[Bundle], Dict[int, int]]:
    parent = {cd["curriculum_id"]: cd["curriculum_id"] for cd in curriculum_data}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cd in curriculum_data:
        mid = cd.get("match_with_curriculum_id")
        if mid is not None:
            union(cd["curriculum_id"], mid)

    groups = collections.defaultdict(list)
    for cd in curriculum_data:
        groups[find(cd["curriculum_id"])].append(cd)

    bundles: List[Bundle] = []
    curriculum_to_bundle: Dict[int, int] = {}
    for bundle_id, items in enumerate(groups.values()):
        lesson_count = items[0]["lessom_week_count"]
        teacher_ids = sorted({cd["teacher_id"] for cd in items})
        subject_ids = sorted({cd["subject_id"] for cd in items})
        curriculum_ids = sorted(cd["curriculum_id"] for cd in items)
        for cid in curriculum_ids:
            curriculum_to_bundle[cid] = bundle_id
        bundles.append(
            Bundle(
                id=bundle_id,
                curriculum_ids=curriculum_ids,
                teacher_ids=teacher_ids,
                subject_ids=subject_ids,
                lesson_count=lesson_count,
            )
        )

    return bundles, curriculum_to_bundle


def _build_slots(group: dict) -> Tuple[List[Slot], List[List[int]]]:
    slots: List[Slot] = []
    day_slots: List[List[int]] = []
    for day_index, ws in enumerate(group["weekday_slots"]):
        slot_ids: List[int] = []
        for pos, lt in enumerate(ws["lesson_times_slots"]):
            slot_id = len(slots)
            slots.append(
                Slot(
                    id=slot_id,
                    day_index=day_index,
                    pos=pos,
                    weekday_id=ws["weekday_id"],
                    weekday_name=ws["weekday_name"],
                    lesson_time_id=lt["lesson_time_id"],
                    start_time=lt["start_time"],
                    end_time=lt["end_time"],
                )
            )
            slot_ids.append(slot_id)
        day_slots.append(slot_ids)
    return slots, day_slots


def _trim_bundle_counts(bundles: List[Bundle], capacity: int) -> Tuple[Dict[int, int], int]:
    counts = {b.id: b.lesson_count for b in bundles}
    required = sum(counts.values())
    if required <= capacity:
        return counts, 0

    excess = required - capacity
    ordered = sorted(bundles, key=lambda b: (-b.lesson_count, b.id))
    while excess > 0:
        progressed = False
        for b in ordered:
            if counts[b.id] > 0 and excess > 0:
                counts[b.id] -= 1
                excess -= 1
                progressed = True
        if not progressed:
            break

    trimmed = required - sum(counts.values())
    return counts, trimmed


def build_model(
    gap_weight: int = 10,
    early_weight: int = 1,
    unassigned_weight: int = 1000,
    over_capacity_strategy: str = "unassigned",
):
    model = cp_model.CpModel()

    # Storage
    x = {}  # (group_id, bundle_id, slot_id) -> BoolVar
    occ = {}  # (group_id, slot_id) -> BoolVar
    gap_vars = []
    objective_terms = []

    teacher_time_map: Dict[Tuple[int, int, int], List] = collections.defaultdict(list)
    busy_slots = set()
    for entry in data.get("teachers_busy", []):
        teacher_id = entry.get("teacher_id", entry.get("staff_id"))
        weekday_id = entry.get("weekday_id")
        lesson_time_id = entry.get("lesson_time_id")
        if teacher_id is None or weekday_id is None or lesson_time_id is None:
            continue
        busy_slots.add((teacher_id, weekday_id, lesson_time_id))
    teacher_week_map: Dict[int, List] = collections.defaultdict(list)
    teacher_max = {
        t["teacher_id"]: t["lesson_week_count_sum"] for t in data["curriculum_teachers"]
    }
    group_info = {}

    for group in data["groups_curriculum"]:
        gid = group["group_id"]
        bundles, curriculum_to_bundle = _build_bundles(group["curriculum_data"])
        slots, day_slots = _build_slots(group)

        group_info[gid] = {
            "group": group,
            "bundles": bundles,
            "curriculum_to_bundle": curriculum_to_bundle,
            "slots": slots,
            "day_slots": day_slots,
        }

        # Create variables
        slot_to_xs: Dict[int, List] = {slot.id: [] for slot in slots}

        for bundle in bundles:
            for slot in slots:
                var = model.new_bool_var(f"x_g{gid}_b{bundle.id}_s{slot.id}")
                x[(gid, bundle.id, slot.id)] = var
                slot_to_xs[slot.id].append(var)

                # Teacher conflict collection
                for teacher_id in bundle.teacher_ids:
                    teacher_time_map[(teacher_id, slot.weekday_id, slot.lesson_time_id)].append(var)
                    teacher_week_map[teacher_id].append(var)

        for slot in slots:
            occ_var = model.new_bool_var(f"occ_g{gid}_s{slot.id}")
            occ[(gid, slot.id)] = occ_var
            # At most one bundle per slot; occ captures if anything scheduled.
            model.add(sum(slot_to_xs[slot.id]) == occ_var)

            # Objective: earlier slots are preferred.
            if early_weight:
                objective_terms.append(early_weight * slot.pos * occ_var)

        # Determine if the group is over capacity (respecting max_per_day).
        max_per_day = group["max_lessons_per_day"]
        required_lessons = sum(bundle.lesson_count for bundle in bundles)
        capacity = sum(min(max_per_day, len(slot_ids)) for slot_ids in day_slots)

        strategy = over_capacity_strategy.lower()
        if strategy not in {"unassigned", "trim"}:
            raise ValueError(
                f"Unknown over_capacity_strategy: {over_capacity_strategy}. "
                "Use 'unassigned' or 'trim'."
            )

        allow_unassigned = strategy == "unassigned" and required_lessons > capacity
        trimmed_counts: Dict[int, int] = {b.id: b.lesson_count for b in bundles}
        trimmed = 0
        if strategy == "trim":
            trimmed_counts, trimmed = _trim_bundle_counts(bundles, capacity)
            if trimmed > 0:
                print(
                    f"Warning: group {gid} requires {required_lessons} lessons but capacity is "
                    f"{capacity}. Trimmed {trimmed} lessons.",
                    file=sys.stderr,
                )
        elif allow_unassigned:
            print(
                f"Warning: group {gid} requires {required_lessons} lessons but capacity is "
                f"{capacity}. Allowing unassigned lessons.",
                file=sys.stderr,
            )

        # Each bundle scheduled weekly count (or less if group is over capacity).
        for bundle in bundles:
            scheduled = sum(x[(gid, bundle.id, slot.id)] for slot in slots)
            target_count = trimmed_counts[bundle.id]
            if allow_unassigned:
                model.add(scheduled <= target_count)
                if unassigned_weight:
                    objective_terms.append(unassigned_weight * (target_count - scheduled))
            else:
                model.add(scheduled == target_count)

        # Max lessons per day.
        for slot_ids in day_slots:
            if not slot_ids:
                continue
            model.add(sum(occ[(gid, sid)] for sid in slot_ids) <= max_per_day)

        # Gaps per day (soft): empty slot between two occupied slots.
        if gap_weight:
            for slot_ids in day_slots:
                n = len(slot_ids)
                if n <= 2:
                    continue
                before = [model.new_bool_var(f"before_g{gid}_d{slot_ids[0]}_{i}") for i in range(n)]
                after = [model.new_bool_var(f"after_g{gid}_d{slot_ids[0]}_{i}") for i in range(n)]
                gaps = [model.new_bool_var(f"gap_g{gid}_d{slot_ids[0]}_{i}") for i in range(n)]

                model.add(before[0] == 0)
                for i in range(1, n):
                    prev_occ = occ[(gid, slot_ids[i - 1])]
                    model.add(before[i] >= before[i - 1])
                    model.add(before[i] >= prev_occ)
                    model.add(before[i] <= before[i - 1] + prev_occ)

                model.add(after[n - 1] == 0)
                for i in range(n - 2, -1, -1):
                    next_occ = occ[(gid, slot_ids[i + 1])]
                    model.add(after[i] >= after[i + 1])
                    model.add(after[i] >= next_occ)
                    model.add(after[i] <= after[i + 1] + next_occ)

                for i in range(n):
                    occ_i = occ[(gid, slot_ids[i])]
                    gap = gaps[i]
                    model.add(gap <= before[i])
                    model.add(gap <= after[i])
                    model.add(gap <= 1 - occ_i)
                    model.add(gap >= before[i] + after[i] + 1 - occ_i - 2)
                    gap_vars.append(gap)
                    objective_terms.append(gap_weight * gap)

    # Teacher conflict constraints across all groups.
    for vars_for_slot in teacher_time_map.values():
        if len(vars_for_slot) > 1:
            model.add(sum(vars_for_slot) <= 1)

    # Teacher availability constraints.
    for key in busy_slots:
        vars_for_slot = teacher_time_map.get(key)
        if vars_for_slot:
            model.add(sum(vars_for_slot) == 0)

    # Teacher weekly load caps.
    for teacher_id, vars_for_teacher in teacher_week_map.items():
        max_week = teacher_max.get(teacher_id)
        if max_week is not None:
            model.add(sum(vars_for_teacher) <= max_week)

    if objective_terms:
        model.minimize(sum(objective_terms))

    return model, x, occ, group_info


def solve(
    time_limit: int = 60,
    gap_weight: int = 10,
    early_weight: int = 1,
    unassigned_weight: int = 1000,
    over_capacity_strategy: str = "unassigned",
    log: bool = False,
):
    model, x, occ, group_info = build_model(
        gap_weight=gap_weight,
        early_weight=early_weight,
        unassigned_weight=unassigned_weight,
        over_capacity_strategy=over_capacity_strategy,
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 8
    if log:
        solver.parameters.log_search_progress = True

    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None, status, None

    schedule = {"groups": {}}
    for gid, info in group_info.items():
        slots = info["slots"]
        bundles = info["bundles"]
        day_slots = info["day_slots"]

        bundle_by_id = {b.id: b for b in bundles}
        group_entry = {
            "group_id": gid,
            "days": {},
        }

        # Precompute slot -> assigned bundle id
        slot_bundle = {}
        for slot in slots:
            assigned = None
            for b in bundles:
                if solver.Value(x[(gid, b.id, slot.id)]):
                    assigned = b.id
                    break
            slot_bundle[slot.id] = assigned

        for day_index, slot_ids in enumerate(day_slots):
            weekday_id = info["group"]["weekday_slots"][day_index]["weekday_id"]
            weekday_name = info["group"]["weekday_slots"][day_index]["weekday_name"]
            entries = []
            for sid in slot_ids:
                slot = slots[sid]
                b_id = slot_bundle[sid]
                if b_id is None:
                    curriculum_ids = []
                    teacher_ids = []
                    subject_ids = []
                else:
                    b = bundle_by_id[b_id]
                    curriculum_ids = b.curriculum_ids
                    teacher_ids = b.teacher_ids
                    subject_ids = b.subject_ids
                entries.append(
                    {
                        "lesson_time_id": slot.lesson_time_id,
                        "start_time": slot.start_time,
                        "end_time": slot.end_time,
                        "curriculum_ids": curriculum_ids,
                        "teacher_ids": teacher_ids,
                        "subject_ids": subject_ids,
                    }
                )

            group_entry["days"][str(weekday_id)] = {
                "weekday_name": weekday_name,
                "slots": entries,
            }

        schedule["groups"][str(gid)] = group_entry

    return schedule, status, solver.ObjectiveValue()


def main():
    parser = argparse.ArgumentParser(description="Generate timetable with OR-Tools CP-SAT")
    parser.add_argument("--time-limit", type=int, default=60, help="Max solve time in seconds")
    parser.add_argument("--gap-weight", type=int, default=10, help="Weight for gap minimization")
    parser.add_argument("--early-weight", type=int, default=1, help="Weight for early-slot preference")
    parser.add_argument(
        "--unassigned-weight",
        type=int,
        default=1000,
        help="Penalty weight for unassigned lessons when a group is over capacity",
    )
    parser.add_argument(
        "--over-capacity-strategy",
        choices=["unassigned", "trim"],
        default="unassigned",
        help="How to handle groups with more required lessons than capacity",
    )
    parser.add_argument("--output", default="schedule.json", help="Output JSON file")
    parser.add_argument(
        "--render",
        nargs="?",
        const="schedule.html",
        help="Render HTML timetable to this path (default: schedule.html)",
    )
    parser.add_argument(
        "--render-group",
        type=int,
        default=None,
        help="Render a single group_id when using --render",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open rendered HTML in the default browser (requires --render)",
    )
    parser.add_argument("--log", action="store_true", help="Enable solver log output")
    args = parser.parse_args()

    schedule, status, objective = solve(
        time_limit=args.time_limit,
        gap_weight=args.gap_weight,
        early_weight=args.early_weight,
        unassigned_weight=args.unassigned_weight,
        over_capacity_strategy=args.over_capacity_strategy,
        log=args.log,
    )

    if schedule is None:
        print(f"No feasible solution found. Status: {status}", file=sys.stderr)
        sys.exit(2)

    with open(args.output, "w") as f:
        json.dump(schedule, f, indent=2)

    print(f"Solved. Status: {status}, objective: {objective}")
    print(f"Wrote schedule to {args.output}")

    if args.render:
        html = render_schedule(schedule, group_id=args.render_group)
        with open(args.render, "w") as f:
            f.write(html)
        print(f"Wrote timetable HTML to {args.render}")
        if args.open:
            path = Path(args.render).resolve()
            webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
