import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional


def _fmt_list(items: List[int]) -> str:
    if not items:
        return "-"
    return ", ".join(str(x) for x in items)


def _fmt_time(value: str) -> str:
    if len(value) >= 5:
        return value[:5]
    return value


def _render_group(group: dict) -> str:
    days: Dict[str, dict] = group["days"]
    day_keys = sorted(days.keys(), key=lambda x: int(x))
    day_names = [days[k]["weekday_name"] for k in day_keys]

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
                    cell = (
                        f"<div class=\"time\">{_fmt_time(slot['start_time'])}-{_fmt_time(slot['end_time'])}</div>"
                        f"<div class=\"meta\"><span>S:</span> {_fmt_list(slot['subject_ids'])}</div>"
                        f"<div class=\"meta\"><span>T:</span> {_fmt_list(slot['teacher_ids'])}</div>"
                        f"<div class=\"meta\"><span>C:</span> {_fmt_list(slot['curriculum_ids'])}</div>"
                    )
                    cell_class = "assigned"
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

    return f"<section><h2>Group {group['group_id']}</h2>{table}</section>"


def render_schedule(schedule: dict, group_id: Optional[int] = None) -> str:
    groups = schedule.get("groups", {})
    if group_id is not None:
        key = str(group_id)
        if key not in groups:
            raise SystemExit(f"Group {group_id} not found in schedule.json")
        groups = {key: groups[key]}

    body_sections = []
    for gid in sorted(groups.keys(), key=lambda x: int(x)):
        body_sections.append(_render_group(groups[gid]))

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
