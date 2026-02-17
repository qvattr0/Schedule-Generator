import argparse
import collections
import json
import sys
import webbrowser
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from ortools.sat.python import cp_model
from mock_data import data as data
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


def analyze_infeasibility(
    input_data: dict,
    over_capacity_strategy: str = "unassigned",
    subject_spread_strategy: str = "soft",
    ignore_availability: bool = False,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "global": {},
        "groups": [],
        "top_reasons": [],
        "primary_cause": None,
        "teacher_global_checks": [],
    }

    busy_slots = set()
    if not ignore_availability:
        for entry in (input_data.get("teachers_busy") or []):
            teacher_id = entry.get("teacher_id", entry.get("staff_id"))
            weekday_id = entry.get("weekday_id")
            lesson_time_id = entry.get("lesson_time_id")
            if teacher_id is None or weekday_id is None or lesson_time_id is None:
                continue
            busy_slots.add((int(teacher_id), int(weekday_id), int(lesson_time_id)))

    teacher_max = {
        int(t["teacher_id"]): int(t["lesson_week_count_sum"])
        for t in (input_data.get("curriculum_teachers") or [])
        if t.get("teacher_id") is not None and t.get("lesson_week_count_sum") is not None
    }

    strategy = str(over_capacity_strategy).lower()
    spread = str(subject_spread_strategy).lower()

    reasons_counter = collections.Counter()
    best_deficit = None  # (deficit_value, group_id, teacher_id, reason, detail)

    teacher_available_week = collections.Counter()
    teacher_seen_slots = collections.defaultdict(set)

    all_unique_times = set()
    groups = input_data.get("groups_curriculum") or []
    for g in groups:
        for ws in (g.get("weekday_slots") or []):
            wd = ws.get("weekday_id")
            for lt in (ws.get("lesson_times_slots") or []):
                all_unique_times.add((int(wd), int(lt["lesson_time_id"])))

    for tid in teacher_max.keys():
        for (wd, ltid) in all_unique_times:
            if (tid, wd, ltid) not in busy_slots:
                teacher_seen_slots[tid].add((wd, ltid))
        teacher_available_week[tid] = len(teacher_seen_slots[tid])

    teacher_required_total = collections.Counter()
    teacher_group_times = collections.defaultdict(set)

    for group in groups:
        gid = int(group.get("group_id", 0))
        curriculum_data = group.get("curriculum_data") or []
        weekday_slots = group.get("weekday_slots") or []
        max_per_day = int(group.get("max_lessons_per_day", 0) or 0)

        g_times = set()
        day_counts = []
        for ws in weekday_slots:
            wd = ws.get("weekday_id")
            lts = ws.get("lesson_times_slots") or []
            day_counts.append(len(lts))
            for lt in lts:
                g_times.add((int(wd), int(lt["lesson_time_id"])))

        bundles, _ = _build_bundles(curriculum_data)
        required_lessons = sum(int(b.lesson_count) for b in bundles)
        capacity = sum(min(max_per_day, c) for c in day_counts) if max_per_day > 0 else 0

        allow_unassigned = (strategy == "unassigned" and required_lessons > capacity)

        g_reasons = []
        g_detail: Dict[str, Any] = {
            "group_id": gid,
            "max_per_day": max_per_day,
            "days": len(weekday_slots),
            "slots_unique": len(g_times),
            "required_lessons": required_lessons,
            "capacity": capacity,
            "over_capacity_strategy": strategy,
            "allow_unassigned": allow_unassigned,
            "subject_spread_strategy": spread,
            "reasons": [],
            "teacher_checks": [],
        }

        if len(g_times) == 0:
            g_reasons.append("NO_SLOTS")
            reasons_counter["NO_SLOTS"] += 1
            if best_deficit is None:
                best_deficit = (10**9, gid, None, "NO_SLOTS", {"slots_unique": 0})

        if max_per_day <= 0:
            g_reasons.append("MAX_PER_DAY_ZERO")
            reasons_counter["MAX_PER_DAY_ZERO"] += 1
            if best_deficit is None:
                best_deficit = (10**8, gid, None, "MAX_PER_DAY_ZERO", {"max_per_day": max_per_day})

        if capacity < required_lessons and not allow_unassigned and strategy != "trim":
            g_reasons.append("CAPACITY_LT_REQUIRED")
            reasons_counter["CAPACITY_LT_REQUIRED"] += 1
            deficit = required_lessons - capacity
            if best_deficit is None or deficit > best_deficit[0]:
                best_deficit = (deficit, gid, None, "CAPACITY_LT_REQUIRED", {"required": required_lessons, "capacity": capacity})

        curriculum_ids = {int(cd["curriculum_id"]) for cd in curriculum_data if cd.get("curriculum_id") is not None}
        missing_match = 0
        for cd in curriculum_data:
            mid = cd.get("match_with_curriculum_id")
            if mid is not None and int(mid) not in curriculum_ids:
                missing_match += 1
        if missing_match:
            g_reasons.append("MATCH_WITH_MISSING")
            reasons_counter["MATCH_WITH_MISSING"] += 1
            g_detail["missing_match_count"] = int(missing_match)

        teacher_required_in_group = collections.Counter()
        for b in bundles:
            for tid in b.teacher_ids:
                teacher_required_in_group[int(tid)] += int(b.lesson_count)
                teacher_required_total[int(tid)] += int(b.lesson_count)
                teacher_group_times[int(tid)].update(g_times)

        for tid, need in teacher_required_in_group.items():
            available = 0
            blocked = 0
            for (wd, ltid) in g_times:
                if (tid, wd, ltid) in busy_slots:
                    blocked += 1
                else:
                    available += 1

            cap_week = teacher_max.get(tid)
            teacher_reason = []

            if available == 0 and need > 0:
                teacher_reason.append("TEACHER_NO_AVAILABLE_SLOTS_IN_GROUP")
                reasons_counter["TEACHER_NO_AVAILABLE_SLOTS_IN_GROUP"] += 1
                deficit = need
                if best_deficit is None or deficit > best_deficit[0]:
                    best_deficit = (deficit, gid, tid, "TEACHER_NO_AVAILABLE_SLOTS_IN_GROUP",
                                    {"need": int(need), "available": int(available), "blocked": int(blocked)})

            if cap_week is not None and need > cap_week:
                teacher_reason.append("TEACHER_WEEK_CAP_TOO_LOW_FOR_SINGLE_GROUP")
                reasons_counter["TEACHER_WEEK_CAP_TOO_LOW_FOR_SINGLE_GROUP"] += 1
                deficit = need - cap_week
                if best_deficit is None or deficit > best_deficit[0]:
                    best_deficit = (deficit, gid, tid, "TEACHER_WEEK_CAP_TOO_LOW_FOR_SINGLE_GROUP",
                                    {"need": int(need), "cap_week": int(cap_week)})

            if need > available and not allow_unassigned:
                teacher_reason.append("TEACHER_NEED_GT_AVAILABLE_TIMES_IN_GROUP")
                reasons_counter["TEACHER_NEED_GT_AVAILABLE_TIMES_IN_GROUP"] += 1
                deficit = need - available
                if best_deficit is None or deficit > best_deficit[0]:
                    best_deficit = (deficit, gid, tid, "TEACHER_NEED_GT_AVAILABLE_TIMES_IN_GROUP",
                                    {"need": int(need), "available": int(available), "blocked": int(blocked)})

            if teacher_reason:
                g_detail["teacher_checks"].append({
                    "teacher_id": tid,
                    "need_lessons_in_group": int(need),
                    "available_times_in_group": int(available),
                    "blocked_busy_times_in_group": int(blocked),
                    "teacher_week_cap": cap_week,
                    "reasons": teacher_reason,
                })

        g_detail["reasons"] = g_reasons
        if g_reasons or g_detail["teacher_checks"]:
            report["groups"].append(g_detail)

    for tid, need_total in teacher_required_total.items():
        cap_week = teacher_max.get(tid)
        avail_week = teacher_available_week.get(tid, 0)
        avail_group = 0
        if tid in teacher_group_times:
            avail_group = sum(
                1 for (wd, ltid) in teacher_group_times[tid]
                if (tid, wd, ltid) not in busy_slots
            )

        reasons = []
        if cap_week is not None and need_total > cap_week:
            reasons.append("TEACHER_TOTAL_DEMAND_GT_WEEK_CAP")
            reasons_counter["TEACHER_TOTAL_DEMAND_GT_WEEK_CAP"] += 1
            deficit = need_total - cap_week
            if best_deficit is None or deficit > best_deficit[0]:
                best_deficit = (deficit, 0, tid, "TEACHER_TOTAL_DEMAND_GT_WEEK_CAP",
                                {"need_total": int(need_total), "cap_week": int(cap_week)})

        if need_total > avail_week:
            reasons.append("TEACHER_TOTAL_DEMAND_GT_AVAILABLE_WEEK_TIMES")
            reasons_counter["TEACHER_TOTAL_DEMAND_GT_AVAILABLE_WEEK_TIMES"] += 1
            deficit = need_total - avail_week
            if best_deficit is None or deficit > best_deficit[0]:
                best_deficit = (deficit, 0, tid, "TEACHER_TOTAL_DEMAND_GT_AVAILABLE_WEEK_TIMES",
                                {"need_total": int(need_total), "available_week_times": int(avail_week)})

        if avail_group and need_total > avail_group:
            reasons.append("TEACHER_TOTAL_DEMAND_GT_GROUP_AVAILABLE_TIMES")
            reasons_counter["TEACHER_TOTAL_DEMAND_GT_GROUP_AVAILABLE_TIMES"] += 1
            deficit = need_total - avail_group
            if best_deficit is None or deficit > best_deficit[0]:
                best_deficit = (deficit, 0, tid, "TEACHER_TOTAL_DEMAND_GT_GROUP_AVAILABLE_TIMES",
                                {"need_total": int(need_total), "available_group_times": int(avail_group)})

        if reasons:
            report["teacher_global_checks"].append({
                "teacher_id": tid,
                "need_total_lessons": int(need_total),
                "teacher_week_cap": cap_week,
                "available_week_times": int(avail_week),
                "available_group_times": int(avail_group),
                "reasons": reasons,
            })

    report["top_reasons"] = [{"reason": r, "count": c} for r, c in reasons_counter.most_common(10)]
    report["global"] = {
        "groups_total": len(groups),
        "teachers_busy_unique": len(busy_slots),
        "teacher_caps_count": len(teacher_max),
        "all_unique_week_times": len(all_unique_times),
    }

    if best_deficit is not None:
        _, gid, tid, reason, detail = best_deficit
        report["primary_cause"] = {
            "group_id": gid if gid != 0 else None,
            "teacher_id": tid,
            "reason": reason,
            "detail": detail,
        }

    return report


def print_feasibility_report(report: Dict[str, Any]) -> None:
    groups_issues = len(report.get("groups", []))
    teacher_issues = len(report.get("teacher_global_checks", []))
    primary = report.get("primary_cause")
    top_reasons = report.get("top_reasons", [])[:5]

    if groups_issues == 0 and teacher_issues == 0 and not primary:
        print("[feasibility] OK (no issues detected)", file=sys.stderr)
        print("[feasibility] Summary: Input appears feasible under current strategies.", file=sys.stderr)
        return

    print(
        f"[feasibility] issues detected (groups={groups_issues}, teacher_global={teacher_issues})",
        file=sys.stderr,
    )
    if primary:
        print(f"[feasibility] primary_cause: {primary}", file=sys.stderr)
    if top_reasons:
        print(f"[feasibility] top_reasons: {top_reasons}", file=sys.stderr)

    summary_parts = []
    if primary:
        reason = primary.get("reason")
        gid = primary.get("group_id")
        tid = primary.get("teacher_id")
        detail = primary.get("detail")
        who = []
        if gid is not None:
            who.append(f"group {gid}")
        if tid is not None:
            who.append(f"teacher {tid}")
        who_str = ", ".join(who) if who else "unknown target"
        summary_parts.append(f"Primary issue: {reason} ({who_str})")
        if detail:
            summary_parts.append(f"detail={detail}")
    if groups_issues:
        summary_parts.append(f"groups_with_issues={groups_issues}")
    if teacher_issues:
        summary_parts.append(f"teacher_global_issues={teacher_issues}")

    summary = "; ".join(summary_parts) if summary_parts else "Issues detected. See above for details."
    print(f"[feasibility] Summary: {summary}", file=sys.stderr)


def build_unsat_core_report(
    solver: cp_model.CpSolver,
    assumption_records: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    core_literals = [int(lit) for lit in solver.sufficient_assumptions_for_infeasibility()]
    category_counts = collections.Counter()
    group_counts = collections.Counter()
    teacher_counts = collections.Counter()
    teacher_time_counts = collections.Counter()
    teacher_group_pair_counts = collections.Counter()
    core_items: List[Dict[str, Any]] = []

    for lit in core_literals:
        record = dict(assumption_records.get(lit, {"category": "UNKNOWN"}))
        record["literal"] = lit
        core_items.append(record)

        category = str(record.get("category", "UNKNOWN"))
        category_counts[category] += 1

        gid = record.get("group_id")
        if gid is not None:
            group_counts[int(gid)] += 1

        tid = record.get("teacher_id")
        if tid is not None:
            teacher_counts[int(tid)] += 1

        wd = record.get("weekday_id")
        ltid = record.get("lesson_time_id")
        if tid is not None and wd is not None and ltid is not None:
            teacher_time_counts[(int(tid), int(wd), int(ltid), category)] += 1

        if tid is not None and gid is not None:
            teacher_group_pair_counts[(int(tid), int(gid))] += 1
        involved_groups = record.get("involved_groups")
        if tid is not None and isinstance(involved_groups, list):
            for g in involved_groups:
                teacher_group_pair_counts[(int(tid), int(g))] += 1

    return {
        "core_size": len(core_items),
        "core_literals": core_literals,
        "categories": [
            {"category": category, "count": count}
            for category, count in category_counts.most_common()
        ],
        "groups": [
            {"group_id": gid, "count": count}
            for gid, count in sorted(group_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "teachers": [
            {"teacher_id": tid, "count": count}
            for tid, count in sorted(teacher_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "teacher_time_slots": [
            {
                "teacher_id": tid,
                "weekday_id": wd,
                "lesson_time_id": ltid,
                "category": category,
                "count": count,
            }
            for (tid, wd, ltid, category), count in sorted(
                teacher_time_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "teacher_group_pairs": [
            {"teacher_id": tid, "group_id": gid, "count": count}
            for (tid, gid), count in sorted(
                teacher_group_pair_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "core_items": core_items,
    }


def print_unsat_core_report(report: Dict[str, Any], max_items: int = 20) -> None:
    core_size = int(report.get("core_size", 0))
    if core_size <= 0:
        print("[unsat-core] No assumptions were returned by the solver.", file=sys.stderr)
        return

    print(
        f"[unsat-core] Extracted sufficient infeasible core with {core_size} assumption(s).",
        file=sys.stderr,
    )

    categories = report.get("categories", [])
    if categories:
        top = ", ".join(
            f"{entry['category']}={entry['count']}" for entry in categories[:8]
        )
        print(f"[unsat-core] categories: {top}", file=sys.stderr)

    groups = report.get("groups", [])
    if groups:
        top = ", ".join(f"g{entry['group_id']}:{entry['count']}" for entry in groups[:8])
        print(f"[unsat-core] groups: {top}", file=sys.stderr)

    teachers = report.get("teachers", [])
    if teachers:
        top = ", ".join(f"t{entry['teacher_id']}:{entry['count']}" for entry in teachers[:8])
        print(f"[unsat-core] teachers: {top}", file=sys.stderr)

    teacher_time_slots = report.get("teacher_time_slots", [])
    if teacher_time_slots:
        top_slots = teacher_time_slots[:8]
        text = ", ".join(
            f"t{entry['teacher_id']}@d{entry['weekday_id']}/lt{entry['lesson_time_id']}"
            f"({entry['category']}:{entry['count']})"
            for entry in top_slots
        )
        print(f"[unsat-core] teacher_time_hotspots: {text}", file=sys.stderr)

    teacher_group_pairs = report.get("teacher_group_pairs", [])
    if teacher_group_pairs:
        top_pairs = teacher_group_pairs[:8]
        text = ", ".join(
            f"t{entry['teacher_id']}<->g{entry['group_id']}({entry['count']})"
            for entry in top_pairs
        )
        print(f"[unsat-core] teacher_group_pairs: {text}", file=sys.stderr)

    core_items = report.get("core_items", [])
    show = max(0, int(max_items))
    for idx, item in enumerate(core_items[:show], start=1):
        print(
            f"[unsat-core] item[{idx}]: {json.dumps(item, sort_keys=True)}",
            file=sys.stderr,
        )
    if len(core_items) > show:
        print(
            f"[unsat-core] ... {len(core_items) - show} more core item(s) omitted.",
            file=sys.stderr,
        )


def build_model(
    gap_weight: int = 10,
    early_weight: int = 1,
    unassigned_weight: int = 1000,
    over_capacity_strategy: str = "unassigned",
    subject_spread_strategy: str = "soft",
    subject_spread_weight: int = 5,
    ignore_availability: bool = False,
    diagnose_unsat: bool = False,
):
    model = cp_model.CpModel()
    assumption_records: Optional[Dict[int, Dict[str, Any]]] = {} if diagnose_unsat else None

    def add_labeled_constraint(expr, category: Optional[str] = None, **meta: Any):
        ct = model.add(expr)
        if assumption_records is not None and category:
            a = model.new_bool_var(f"assume_{len(assumption_records)}_{category}")
            ct.only_enforce_if(a)
            model.add_assumption(a)
            payload = {"category": category}
            payload.update(meta)
            assumption_records[a.Index()] = payload
        return ct

    # Storage
    x = {}  # (group_id, bundle_id, slot_id) -> BoolVar
    occ = {}  # (group_id, slot_id) -> BoolVar
    gap_vars = []
    objective_terms = []

    teacher_time_map: Dict[Tuple[int, int, int], List] = collections.defaultdict(list)
    busy_slots = set()
    if not ignore_availability:
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

        subject_to_bundles: Dict[int, List[int]] = collections.defaultdict(list)
        for bundle in bundles:
            for subject_id in bundle.subject_ids:
                subject_to_bundles[subject_id].append(bundle.id)

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

        spread_strategy = subject_spread_strategy.lower()
        if spread_strategy not in {"off", "soft", "hard", "both"}:
            raise ValueError(
                f"Unknown subject_spread_strategy: {subject_spread_strategy}. "
                "Use 'off', 'soft', 'hard', or 'both'."
            )
        spread_soft = spread_strategy in {"soft", "both"}
        spread_hard = spread_strategy in {"hard", "both"}

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
                add_labeled_constraint(
                    scheduled <= target_count,
                    category="group_bundle_target",
                    group_id=gid,
                    bundle_id=bundle.id,
                    relation="<=",
                    target_count=int(target_count),
                )
                if unassigned_weight:
                    objective_terms.append(unassigned_weight * (target_count - scheduled))
            else:
                add_labeled_constraint(
                    scheduled == target_count,
                    category="group_bundle_target",
                    group_id=gid,
                    bundle_id=bundle.id,
                    relation="==",
                    target_count=int(target_count),
                )

        # Subject distribution constraints/penalties within a day.
        if spread_soft or spread_hard:
            num_days = max(1, len(day_slots))
            subject_target_counts = {
                subject_id: sum(trimmed_counts[b_id] for b_id in bundle_ids)
                for subject_id, bundle_ids in subject_to_bundles.items()
            }

            for subject_id, bundle_ids in subject_to_bundles.items():
                if not bundle_ids:
                    continue
                required_for_subject = subject_target_counts.get(subject_id, 0)
                if required_for_subject <= 0:
                    continue
                max_per_day_subject = (required_for_subject + num_days - 1) // num_days

                for day_index, slot_ids in enumerate(day_slots):
                    # Build subject occurrence vars/exprs for this day.
                    occ_vars = []
                    occ_exprs = []
                    for sid in slot_ids:
                        vars_for_slot = [x[(gid, b_id, sid)] for b_id in bundle_ids]
                        if spread_soft:
                            if len(vars_for_slot) == 1:
                                occ_var = vars_for_slot[0]
                            else:
                                occ_var = model.new_bool_var(
                                    f"subj_g{gid}_s{subject_id}_slot{sid}"
                                )
                                model.add(sum(vars_for_slot) == occ_var)
                            occ_vars.append(occ_var)
                        if spread_hard:
                            occ_exprs.append(sum(vars_for_slot))

                    if spread_hard and occ_exprs:
                        weekday_id = slots[slot_ids[0]].weekday_id if slot_ids else None
                        add_labeled_constraint(
                            sum(occ_exprs) <= max_per_day_subject,
                            category="group_subject_daily_limit",
                            group_id=gid,
                            subject_id=subject_id,
                            day_index=day_index,
                            weekday_id=weekday_id,
                            max_per_day_subject=int(max_per_day_subject),
                        )

                    if spread_soft and len(occ_vars) >= 2:
                        day_len = len(occ_vars)
                        for i in range(day_len - 1):
                            for j in range(i + 1, day_len):
                                dist = j - i
                                weight = (day_len - dist)
                                both = model.new_bool_var(
                                    f"subj_pair_g{gid}_s{subject_id}_d{day_index}_{i}_{j}"
                                )
                                model.add(both <= occ_vars[i])
                                model.add(both <= occ_vars[j])
                                model.add(both >= occ_vars[i] + occ_vars[j] - 1)
                                if subject_spread_weight:
                                    objective_terms.append(subject_spread_weight * weight * both)

        # Max lessons per day.
        for slot_ids in day_slots:
            if not slot_ids:
                continue
            weekday_id = slots[slot_ids[0]].weekday_id
            add_labeled_constraint(
                sum(occ[(gid, sid)] for sid in slot_ids) <= max_per_day,
                category="group_max_lessons_per_day",
                group_id=gid,
                weekday_id=weekday_id,
                max_per_day=int(max_per_day),
                slots_in_day=len(slot_ids),
            )

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
    subject_spread_strategy: str = "soft",
    subject_spread_weight: int = 5,
    ignore_availability: bool = False,
    diagnose_unsat: bool = False,
    unsat_core_max_items: int = 20,
    log: bool = False,
):
    model, x, occ, group_info = build_model(
        gap_weight=gap_weight,
        early_weight=early_weight,
        unassigned_weight=unassigned_weight,
        over_capacity_strategy=over_capacity_strategy,
        subject_spread_strategy=subject_spread_strategy,
        subject_spread_weight=subject_spread_weight,
        ignore_availability=ignore_availability,
        diagnose_unsat=diagnose_unsat,
    )
    report = analyze_infeasibility(
        data,
        over_capacity_strategy=over_capacity_strategy,
        subject_spread_strategy=subject_spread_strategy,
        ignore_availability=ignore_availability,
    )
    print_feasibility_report(report)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 16
    if log:
        solver.parameters.log_search_progress = True

    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status == cp_model.INFEASIBLE and diagnose_unsat and assumption_records:
            core_report = build_unsat_core_report(solver, assumption_records)
            print_unsat_core_report(core_report, max_items=unsat_core_max_items)
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
    parser.add_argument(
        "--subject-spread-strategy",
        choices=["off", "soft", "hard", "both"],
        default="soft",
        help="Strategy for spreading same-subject lessons within a week",
    )
    parser.add_argument(
        "--subject-spread-weight",
        type=int,
        default=5,
        help="Penalty weight for same-subject proximity (soft/both strategies)",
    )
    parser.add_argument(
        "--ignore-availability",
        action="store_true",
        help="Ignore teachers_busy constraints when generating a schedule",
    )
    parser.add_argument(
        "--diagnose-unsat",
        action="store_true",
        help="When infeasible, extract and print an assumption-based unsat core summary",
    )
    parser.add_argument(
        "--unsat-core-max-items",
        type=int,
        default=20,
        help="How many individual unsat-core constraints to print when --diagnose-unsat is enabled",
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
        subject_spread_strategy=args.subject_spread_strategy,
        subject_spread_weight=args.subject_spread_weight,
        ignore_availability=args.ignore_availability,
        diagnose_unsat=args.diagnose_unsat,
        unsat_core_max_items=args.unsat_core_max_items,
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
