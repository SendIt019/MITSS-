"""Human-readable views of a validated schedule.

Three renderings, all stdlib: an aligned text table, a comma-separated values
(CSV) export, and an ASCII timeline that shows resource contention at a glance.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List

from .model import Assignment, Plan, Schedule, fmt_dt


def render_table(plan: Plan, schedule: Schedule) -> str:
    """Assignments in start order, aligned into columns."""
    rows = [("START", "END", "MIN", "TASK", "RESOURCE", "NAME")]
    for assignment in sorted(schedule.assignments, key=lambda a: (a.start, a.task_id)):
        task = plan.task(assignment.task_id)
        rows.append(
            (
                fmt_dt(assignment.start),
                fmt_dt(assignment.end),
                str(assignment.window.minutes),
                assignment.task_id,
                assignment.resource_id,
                task.name if task else "?",
            )
        )

    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for index, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        lines.append(line)
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))

    if schedule.unscheduled:
        lines.append("")
        lines.append("UNSCHEDULED")
        lines.append("-" * 11)
        for entry in schedule.unscheduled:
            reason = entry.get("reason") or "(no reason given)"
            lines.append(f"  {entry['task_id']}: {reason}")

    return "\n".join(lines)


def render_csv(plan: Plan, schedule: Schedule) -> str:
    """Flat export suitable for a spreadsheet."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["task_id", "task_name", "resource_id", "resource_name", "start", "end",
         "duration_minutes", "status", "reason"]
    )
    for assignment in sorted(schedule.assignments, key=lambda a: (a.start, a.task_id)):
        task = plan.task(assignment.task_id)
        resource = plan.resource(assignment.resource_id)
        writer.writerow(
            [
                assignment.task_id,
                task.name if task else "",
                assignment.resource_id,
                resource.name if resource else "",
                fmt_dt(assignment.start),
                fmt_dt(assignment.end),
                assignment.window.minutes,
                "scheduled",
                "",
            ]
        )
    for entry in schedule.unscheduled:
        task = plan.task(entry["task_id"])
        writer.writerow(
            [
                entry["task_id"],
                task.name if task else "",
                "", "", "", "",
                task.duration_minutes if task else "",
                "unscheduled",
                entry.get("reason", ""),
            ]
        )
    return buffer.getvalue()


def render_timeline(plan: Plan, schedule: Schedule, width: int = 72) -> str:
    """One row per resource, time running left to right across the horizon."""
    horizon_minutes = plan.horizon.minutes
    if horizon_minutes <= 0:
        return "(empty horizon)"

    per_cell = max(1, horizon_minutes // width)
    cells = max(1, min(width, horizon_minutes // per_cell))

    label_width = max((len(r.id) for r in plan.resources), default=8)
    label_width = max(label_width, 8)

    symbols = _symbol_map(schedule)
    lines: List[str] = []

    header_left = fmt_dt(plan.horizon.start)
    header_right = fmt_dt(plan.horizon.end)
    lines.append(" " * (label_width + 2) + f"{header_left} -> {header_right}")
    lines.append(" " * (label_width + 2) + "+" + "-" * cells + "+")

    by_resource: Dict[str, List[Assignment]] = defaultdict(list)
    for assignment in schedule.assignments:
        by_resource[assignment.resource_id].append(assignment)

    for resource in plan.resources:
        # Collect which assignments touch each cell, then decide the character.
        # A cell can be touched by two assignments purely because the cell is
        # coarser than the gap between them; that is rounding, not a conflict.
        # Only genuinely overlapping intervals get the '!' marker.
        occupants: List[List[Assignment]] = [[] for _ in range(cells)]
        for assignment in by_resource.get(resource.id, []):
            start_offset = int(
                (assignment.start - plan.horizon.start).total_seconds() // 60
            )
            end_offset = int(
                (assignment.end - plan.horizon.start).total_seconds() // 60
            )
            first = max(0, start_offset // per_cell)
            last = min(cells - 1, max(first, (end_offset - 1) // per_cell))
            for index in range(first, last + 1):
                if 0 <= index < cells:
                    occupants[index].append(assignment)

        row = []
        for index, here in enumerate(occupants):
            if not here:
                row.append(" ")
                continue
            if len(here) > 1 and _any_real_overlap(here):
                row.append("!")
                continue
            dominant = _dominant_in_cell(here, plan, index, per_cell)
            row.append(symbols.get(dominant.task_id, "#"))
        lines.append(f"{resource.id.ljust(label_width)}  |{''.join(row)}|")

    lines.append(" " * (label_width + 2) + "+" + "-" * cells + "+")
    lines.append("")
    note = f"each cell is about {per_cell} minutes"
    if any("!" in line for line in lines):
        note += "; '!' marks overlapping work"
    lines.append(note)
    lines.append("")
    legend = ", ".join(f"{sym}={tid}" for tid, sym in sorted(symbols.items(), key=lambda kv: kv[1]))
    if legend:
        lines.append(f"legend: {legend}")
    return "\n".join(lines)


def _any_real_overlap(assignments: List[Assignment]) -> bool:
    """True only if two of these assignments actually overlap in time."""
    for i, first in enumerate(assignments):
        for second in assignments[i + 1:]:
            if first.window.overlaps(second.window):
                return True
    return False


def _dominant_in_cell(assignments: List[Assignment], plan: Plan, index: int,
                      per_cell: int) -> Assignment:
    """Whichever assignment fills the most of this cell wins the character."""
    cell_start = plan.horizon.start + timedelta(minutes=index * per_cell)
    cell_end = cell_start + timedelta(minutes=per_cell)

    def coverage(assignment: Assignment) -> int:
        start = max(assignment.start, cell_start)
        end = min(assignment.end, cell_end)
        return max(0, int((end - start).total_seconds() // 60))

    return max(assignments, key=coverage)


def _symbol_map(schedule: Schedule) -> Dict[str, str]:
    """Give each task a stable single character for the timeline."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    task_ids = sorted({a.task_id for a in schedule.assignments})
    return {tid: alphabet[i % len(alphabet)] for i, tid in enumerate(task_ids)}


def render_summary(summary: Dict[str, object]) -> str:
    """Plain-language version of the metrics block."""
    lines = [
        f"tasks scheduled : {summary['tasks_scheduled']} of {summary['tasks_total']}",
        f"makespan        : {summary['makespan_minutes']} min",
        f"finishes        : {summary['finish_time'] or '(nothing scheduled)'}",
        "utilization     :",
    ]
    utilization = summary.get("resource_utilization", {})
    if isinstance(utilization, dict):
        for resource_id, stats in sorted(utilization.items()):
            lines.append(
                f"    {resource_id}: {stats['busy_minutes']} min "
                f"({stats['utilization_pct']}% of horizon capacity)"
            )
    return "\n".join(lines)
