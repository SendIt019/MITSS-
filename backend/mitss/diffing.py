"""Compare two runs of the same plan.

Consistency testing is the point: feed the same plan twice and see whether the
model placed the work the same way. Differences are reported per task rather
than as a line-by-line text diff, so noise like key ordering never shows up.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .model import Schedule, fmt_dt

ADDED = "added"
REMOVED = "removed"
MOVED = "moved"
REASSIGNED = "reassigned"
UNCHANGED = "unchanged"


def _window(assignment) -> str:
    """Compact 'start to end' label, collapsing the shared date when possible."""
    start = fmt_dt(assignment.start)
    end = fmt_dt(assignment.end)
    if start[:10] == end[:10]:
        return f"{start}-{end[11:]}"
    return f"{start}-{end}"


def diff_schedules(before: Schedule, after: Schedule) -> List[Dict[str, Any]]:
    """Return one record per task whose placement differs between runs."""
    left = {a.task_id: a for a in before.assignments}
    right = {a.task_id: a for a in after.assignments}
    changes: List[Dict[str, Any]] = []

    for task_id in sorted(set(left) | set(right)):
        old = left.get(task_id)
        new = right.get(task_id)

        if old is None and new is not None:
            changes.append(
                {
                    "task_id": task_id,
                    "change": ADDED,
                    "detail": f"now scheduled on {new.resource_id} at {fmt_dt(new.start)}",
                }
            )
            continue
        if old is not None and new is None:
            changes.append(
                {
                    "task_id": task_id,
                    "change": REMOVED,
                    "detail": f"was on {old.resource_id} at {fmt_dt(old.start)}, now absent",
                }
            )
            continue
        if old is None or new is None:
            continue

        moved = old.start != new.start or old.end != new.end
        reassigned = old.resource_id != new.resource_id

        if moved and reassigned:
            changes.append(
                {
                    "task_id": task_id,
                    "change": REASSIGNED,
                    "detail": (
                        f"{old.resource_id} {_window(old)} -> "
                        f"{new.resource_id} {_window(new)}"
                    ),
                }
            )
        elif reassigned:
            changes.append(
                {
                    "task_id": task_id,
                    "change": REASSIGNED,
                    "detail": f"{old.resource_id} -> {new.resource_id} (same time)",
                }
            )
        elif moved:
            # Show whole windows, not just starts: a task can keep its start
            # time and still change because its end moved.
            changes.append(
                {
                    "task_id": task_id,
                    "change": MOVED,
                    "detail": f"{_window(old)} -> {_window(new)} (same resource)",
                }
            )

    return changes


def diff_unscheduled(before: Schedule, after: Schedule) -> List[Dict[str, Any]]:
    """Track tasks that entered or left the unscheduled list."""
    left = {e["task_id"] for e in before.unscheduled}
    right = {e["task_id"] for e in after.unscheduled}
    changes: List[Dict[str, Any]] = []
    for task_id in sorted(right - left):
        changes.append({"task_id": task_id, "change": "now_unscheduled", "detail": ""})
    for task_id in sorted(left - right):
        changes.append({"task_id": task_id, "change": "no_longer_unscheduled", "detail": ""})
    return changes


def render_diff(before: Schedule, after: Schedule, label_a: str = "A",
                label_b: str = "B") -> str:
    """Readable report of everything that changed between two runs."""
    changes = diff_schedules(before, after) + diff_unscheduled(before, after)

    header = [
        f"comparing {label_a} -> {label_b}",
        f"  {label_a}: {len(before.assignments)} assignments, "
        f"makespan {before.makespan_minutes} min",
        f"  {label_b}: {len(after.assignments)} assignments, "
        f"makespan {after.makespan_minutes} min",
        "",
    ]

    if not changes:
        return "\n".join(header + ["identical placement - the two runs agree on every task"])

    width = max(len(c["task_id"]) for c in changes)
    body = [f"{len(changes)} task(s) differ:", ""]
    for change in changes:
        body.append(
            f"  {change['task_id'].ljust(width)}  {change['change']:<22} {change['detail']}"
        )
    return "\n".join(header + body)
