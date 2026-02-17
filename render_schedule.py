import argparse
import html
import json
from pathlib import Path
from typing import Dict, Optional, Tuple


def _fmt_time(value: str) -> str:
    if len(value) >= 5:
        return value[:5]
    return value


def _load_name_maps() -> Tuple[
    Dict[int, Dict[int, str]],
    Dict[int, str],
    Dict[int, str],
    Dict[int, str],
]:
    try:
        from mock_data import data as mock_data
    except Exception:
        return {}, {}, {}, {}

    subject_by_group: Dict[int, Dict[int, str]] = {}
    subject_by_id: Dict[int, str] = {}
    for item in mock_data.get("curriculum_subjects", []):
        subject_id = item.get("subject_id")
        subject_name = item.get("subject_name")
        if subject_id is None or not subject_name:
            continue
        subject_by_id.setdefault(int(subject_id), subject_name)
        group_id = item.get("group_id")
        if group_id is None:
            continue
        subject_by_group.setdefault(int(group_id), {})
        subject_by_group[int(group_id)].setdefault(int(subject_id), subject_name)

    teacher_by_id: Dict[int, str] = {}
    for item in mock_data.get("curriculum_teachers", []):
        teacher_id = item.get("teacher_id")
        teacher_name = item.get("teacher_name")
        if teacher_id is None or not teacher_name:
            continue
        teacher_by_id.setdefault(int(teacher_id), teacher_name)

    group_name_by_id: Dict[int, str] = {}
    for item in mock_data.get("groups_curriculum", []):
        group_id = item.get("group_id")
        group_name = item.get("group_name")
        if group_id is None or not group_name:
            continue
        group_name_by_id.setdefault(int(group_id), str(group_name))

    return subject_by_group, subject_by_id, teacher_by_id, group_name_by_id


def _subject_label(
    subject_id: Optional[int],
    group_id: int,
    subject_by_group: Dict[int, Dict[int, str]],
    subject_by_id: Dict[int, str],
) -> str:
    if subject_id is None:
        return "Unknown subject"
    group_subjects = subject_by_group.get(group_id, {})
    name = group_subjects.get(subject_id) or subject_by_id.get(subject_id)
    return name or "Unknown subject"


def _teacher_label(teacher_id: Optional[int], teacher_by_id: Dict[int, str]) -> str:
    if teacher_id is None:
        return "Unknown teacher"
    return teacher_by_id.get(teacher_id) or "Unknown teacher"


def _build_slot_entries(
    slot: dict,
    group_id: int,
    subject_by_group: Dict[int, Dict[int, str]],
    subject_by_id: Dict[int, str],
    teacher_by_id: Dict[int, str],
) -> list:
    subject_ids = slot.get("subject_ids", [])
    teacher_ids = slot.get("teacher_ids", [])

    if not subject_ids and not teacher_ids:
        return []

    if len(subject_ids) == len(teacher_ids):
        pairs = list(zip(subject_ids, teacher_ids))
    elif len(subject_ids) == 1 and len(teacher_ids) > 1:
        pairs = [(subject_ids[0], teacher_id) for teacher_id in teacher_ids]
    elif len(teacher_ids) == 1 and len(subject_ids) > 1:
        pairs = [(subject_id, teacher_ids[0]) for subject_id in subject_ids]
    else:
        count = max(len(subject_ids), len(teacher_ids))
        pairs = [
            (
                subject_ids[i] if i < len(subject_ids) else None,
                teacher_ids[i] if i < len(teacher_ids) else None,
            )
            for i in range(count)
        ]

    entries = []
    for subject_id, teacher_id in pairs:
        entries.append(
            {
                "subject_id": subject_id,
                "subject_name": _subject_label(subject_id, group_id, subject_by_group, subject_by_id),
                "teacher_name": _teacher_label(teacher_id, teacher_by_id),
            }
        )
    return entries


def _render_group(
    group: dict,
    subject_by_group: Dict[int, Dict[int, str]],
    subject_by_id: Dict[int, str],
    teacher_by_id: Dict[int, str],
    group_name_by_id: Dict[int, str],
) -> str:
    group_id = int(group["group_id"])
    group_name = str(
        group.get("group_name")
        or group_name_by_id.get(group_id)
        or f"Group {group_id}"
    )
    days: Dict[str, dict] = group["days"]
    day_keys = sorted(days.keys(), key=lambda x: int(x))
    day_names = [days[k]["weekday_name"] for k in day_keys]

    subject_counts_by_day: Dict[str, Dict[int, int]] = {}
    for k in day_keys:
        counts: Dict[int, int] = {}
        for slot in days[k]["slots"]:
            for subject_id in slot["subject_ids"]:
                counts[subject_id] = counts.get(subject_id, 0) + 1
        subject_counts_by_day[k] = counts

    max_slots = 0
    for k in day_keys:
        max_slots = max(max_slots, len(days[k]["slots"]))

    rows = []
    for i in range(max_slots):
        cells = []
        for k in day_keys:
            slots = days[k]["slots"]
            if i < len(slots):
                slot = slots[i]
                if slot["curriculum_ids"]:
                    repeat = False
                    for subject_id in slot["subject_ids"]:
                        if subject_counts_by_day[k].get(subject_id, 0) >= 2:
                            repeat = True
                            break
                    entries = _build_slot_entries(
                        slot,
                        group_id,
                        subject_by_group,
                        subject_by_id,
                        teacher_by_id,
                    )
                    items = []
                    for entry in entries:
                        subject_name = html.escape(entry["subject_name"])
                        teacher_name = html.escape(entry["teacher_name"])
                        subject_id = entry["subject_id"]
                        tooltip = f"(ID: {subject_id})" if subject_id is not None else ""
                        tooltip_attr = f' title="{html.escape(tooltip)}"' if tooltip else ""
                        items.append(
                            "<div class=\"slot-item\">"
                            f"<div class=\"subject\"{tooltip_attr}>{subject_name}</div>"
                            f"<div class=\"teacher\">{teacher_name}</div>"
                            "</div>"
                        )
                    column_count = max(len(items), 1)
                    cell = (
                        f"<div class=\"time\">{_fmt_time(slot['start_time'])}-{_fmt_time(slot['end_time'])}</div>"
                        f"<div class=\"slot-split\" style=\"--slot-columns: {column_count};\">"
                        f"{''.join(items)}"
                        "</div>"
                    )
                    cell_class = "assigned repeat" if repeat else "assigned"
                else:
                    prev_assigned = False
                    next_assigned = False
                    if i > 0 and i - 1 < len(slots):
                        prev_assigned = bool(slots[i - 1]["curriculum_ids"])
                    if i + 1 < len(slots):
                        next_assigned = bool(slots[i + 1]["curriculum_ids"])
                    gap_class = "gap" if (prev_assigned and next_assigned) else "free"
                    cell = (
                        f"<div class=\"time\">{_fmt_time(slot['start_time'])}-{_fmt_time(slot['end_time'])}</div>"
                        "<div class=\"empty\">Unassigned</div>"
                    )
                    cell_class = gap_class
            else:
                cell = "<div class=\"empty\">-</div>"
                cell_class = "free"
            cells.append(f"<td class=\"{cell_class}\">{cell}</td>")
        rows.append(f"<tr><th class=\"slot\">{i + 1}</th>{''.join(cells)}</tr>")

    header = "".join(f"<th>{name}</th>" for name in day_names)
    table = (
        "<table>"
        f"<thead><tr><th class=\"slot\">Slot</th>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )

    title = f"{html.escape(group_name)} GID:{group_id}"
    return f"<section><h2>{title}</h2>{table}</section>"


def render_schedule(schedule: dict, group_id: Optional[int] = None) -> str:
    subject_by_group, subject_by_id, teacher_by_id, group_name_by_id = _load_name_maps()
    groups = schedule.get("groups", {})
    if group_id is not None:
        key = str(group_id)
        if key not in groups:
            raise SystemExit(f"Group {group_id} not found in schedule.json")
        groups = {key: groups[key]}

    body_sections = []
    for gid in sorted(groups.keys(), key=lambda x: int(x)):
        body_sections.append(
            _render_group(
                groups[gid],
                subject_by_group,
                subject_by_id,
                teacher_by_id,
                group_name_by_id,
            )
        )

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Schedule</title>
  <style>
    :root {{
      --bg: #f7f5f0;
      --ink: #1f1f1f;
      --muted: #6c6c6c;
      --line: #d9d2c7;
      --accent: #0b6e4f;
      --paper: #ffffff;
    }}
    body {{
      margin: 24px;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-serif, Georgia, "Times New Roman", serif;
    }}
    h1 {{
      font-size: 28px;
      margin: 0 0 16px;
    }}
    h2 {{
      margin: 32px 0 12px;
      font-size: 22px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--paper);
      border: 1px solid var(--line);
    }}
    th, td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      vertical-align: top;
    }}
    th {{
      text-align: left;
      background: #efe9df;
      font-weight: 600;
    }}
    th.slot {{
      width: 70px;
      text-align: center;
    }}
    td {{
      min-width: 110px;
    }}
    .time {{
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--accent);
    }}
    .meta {{
      font-size: 12px;
      color: var(--muted);
      margin-top: 2px;
    }}
    .meta span {{
      color: #3c3c3c;
      font-weight: 600;
    }}
    .slot-split {{
      display: grid;
      grid-template-columns: repeat(var(--slot-columns, 1), minmax(0, 1fr));
      gap: 6px;
      margin-top: 4px;
    }}
    .slot-item {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      background: #faf7f1;
    }}
    .subject {{
      font-weight: 600;
      margin-bottom: 2px;
      cursor: help;
    }}
    .teacher {{
      font-size: 12px;
      color: var(--muted);
    }}
    .empty {{
      color: var(--muted);
      font-style: italic;
      font-size: 12px;
    }}
    td.free {{
      background: #f3e6fb;
    }}
    td.gap {{
      background: #f6c4c4;
    }}
    td.repeat {{
      background: #ccebd7;
    }}
    section {{
      margin-bottom: 32px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 24px;
    }}
    @media (max-width: 900px) {{
      .grid {{
        grid-template-columns: 1fr;
      }}
    }}
    section {{
      min-width: 0;
    }}
    td {{
      overflow-wrap: anywhere;
    }}
  </style>
</head>
<body>
  <h1>Generated Timetable</h1>
  <div class="grid">
    {''.join(body_sections)}
  </div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render schedule.json to HTML")
    parser.add_argument("--input", default="schedule.json", help="Input JSON schedule file")
    parser.add_argument("--output", default="schedule.html", help="Output HTML file")
    parser.add_argument("--group", type=int, default=None, help="Render a single group_id")
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text())
    html = render_schedule(data, group_id=args.group)
    Path(args.output).write_text(html)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
