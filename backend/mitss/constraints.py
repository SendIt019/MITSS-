"""Hard-constraint checking: is this schedule actually legal?

validate.py already confirmed the output parses and has the right types. This
module answers the harder question — does the schedule obey the rules the plan
laid down? A model can return perfectly well-formed JavaScript Object Notation
(JSON) that double-books a resource or starts a task before its predecessor
finishes, and that is exactly what this catches.

Each checker is independent and appends to a shared issue list, so one pass
reports every violation rather than the first one.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

from .issues import ERROR, Issue, error, info, warn
from .model import Assignment, Plan, Schedule, fmt_dt


def check_constraints(plan: Plan, schedule: Schedule) -> List[Issue]:
    """Run every hard-constraint check and return all violations found."""
    issues: List[Issue] = []
    _check_references(plan, schedule, issues)
    _check_coverage(plan, schedule, issues)
    _check_durations(plan, schedule, issues)
    _check_horizon(plan, schedule, issues)
    _check_task_windows(plan, schedule, issues)
    _check_required_resources(plan, schedule, issues)
    _check_dependencies(plan, schedule, issues)
    _check_capacity(plan, schedule, issues)
    _check_availability(plan, schedule, issues)
    _check_granularity(plan, schedule, issues)
    return issues


def _where(assignment: Assignment) -> str:
    return f"assignment:{assignment.task_id}"


def _check_references(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Every id the model used must exist in the plan."""
    known_tasks = set(plan.task_ids)
    known_resources = set(plan.resource_ids)

    for assignment in schedule.assignments:
        if assignment.task_id not in known_tasks:
            issues.append(
                error(
                    "unknown_task",
                    f"scheduled task '{assignment.task_id}' is not in the plan",
                    _where(assignment),
                )
            )
        if assignment.resource_id not in known_resources:
            issues.append(
                error(
                    "unknown_resource",
                    f"assigned to resource '{assignment.resource_id}', which is not in the plan",
                    _where(assignment),
                )
            )

    for entry in schedule.unscheduled:
        if entry["task_id"] not in known_tasks:
            issues.append(
                error(
                    "unknown_task",
                    f"unscheduled entry references unknown task '{entry['task_id']}'",
                    "schedule.unscheduled",
                )
            )


def _check_coverage(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Each task must be scheduled exactly once, or explicitly declared unscheduled."""
    counts: Dict[str, int] = defaultdict(int)
    for assignment in schedule.assignments:
        counts[assignment.task_id] += 1

    declared_unscheduled = {e["task_id"] for e in schedule.unscheduled}

    for task in plan.tasks:
        placed = counts.get(task.id, 0)
        if placed == 0 and task.id not in declared_unscheduled:
            issues.append(
                error(
                    "task_missing",
                    f"task '{task.id}' ({task.name}) is neither scheduled nor listed as unscheduled",
                    f"task:{task.id}",
                )
            )
        elif placed > 1:
            issues.append(
                error(
                    "task_scheduled_twice",
                    f"task '{task.id}' appears in {placed} assignments; each task is placed once",
                    f"task:{task.id}",
                )
            )
        if placed >= 1 and task.id in declared_unscheduled:
            issues.append(
                error(
                    "contradictory_status",
                    f"task '{task.id}' is both scheduled and listed as unscheduled",
                    f"task:{task.id}",
                )
            )

    for entry in schedule.unscheduled:
        if entry["task_id"] in set(plan.task_ids) and not entry.get("reason"):
            issues.append(
                warn(
                    "unscheduled_without_reason",
                    f"task '{entry['task_id']}' was dropped with no reason given",
                    "schedule.unscheduled",
                )
            )
        elif entry["task_id"] in set(plan.task_ids):
            issues.append(
                info(
                    "task_unscheduled",
                    f"task '{entry['task_id']}' left unscheduled: {entry['reason']}",
                    "schedule.unscheduled",
                )
            )


def _check_durations(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """A scheduled block must last exactly as long as the task requires."""
    for assignment in schedule.assignments:
        task = plan.task(assignment.task_id)
        if task is None:
            continue  # already reported by _check_references
        actual = assignment.window.minutes
        if actual != task.duration_minutes:
            issues.append(
                error(
                    "duration_mismatch",
                    f"task '{task.id}' needs {task.duration_minutes} min but was given "
                    f"{actual} min ({fmt_dt(assignment.start)} to {fmt_dt(assignment.end)})",
                    _where(assignment),
                )
            )


def _check_horizon(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Nothing may fall outside the planning window."""
    for assignment in schedule.assignments:
        if not plan.horizon.contains(assignment.window):
            issues.append(
                error(
                    "outside_horizon",
                    f"task '{assignment.task_id}' runs {fmt_dt(assignment.start)} to "
                    f"{fmt_dt(assignment.end)}, outside the horizon "
                    f"{fmt_dt(plan.horizon.start)} to {fmt_dt(plan.horizon.end)}",
                    _where(assignment),
                )
            )


def _check_task_windows(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Respect per-task earliest_start and deadline."""
    for assignment in schedule.assignments:
        task = plan.task(assignment.task_id)
        if task is None:
            continue
        if task.earliest_start and assignment.start < task.earliest_start:
            issues.append(
                error(
                    "before_earliest_start",
                    f"task '{task.id}' starts {fmt_dt(assignment.start)}, before its "
                    f"earliest start {fmt_dt(task.earliest_start)}",
                    _where(assignment),
                )
            )
        if task.deadline and assignment.end > task.deadline:
            issues.append(
                error(
                    "past_deadline",
                    f"task '{task.id}' ends {fmt_dt(assignment.end)}, after its deadline "
                    f"{fmt_dt(task.deadline)}",
                    _where(assignment),
                )
            )


def _check_required_resources(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """If a task names eligible resources, it must be assigned to one of them."""
    for assignment in schedule.assignments:
        task = plan.task(assignment.task_id)
        if task is None or not task.requires:
            continue
        if assignment.resource_id not in task.requires:
            issues.append(
                error(
                    "ineligible_resource",
                    f"task '{task.id}' requires one of {task.requires} but was assigned to "
                    f"'{assignment.resource_id}'",
                    _where(assignment),
                )
            )


def _check_dependencies(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """A task may not start until everything it depends on has finished."""
    ends: Dict[str, datetime] = {}
    starts: Dict[str, datetime] = {}
    for assignment in schedule.assignments:
        ends[assignment.task_id] = assignment.end
        starts[assignment.task_id] = assignment.start

    for task in plan.tasks:
        if task.id not in starts:
            continue
        for dep in task.depends_on:
            if dep not in ends:
                issues.append(
                    error(
                        "dependency_unscheduled",
                        f"task '{task.id}' depends on '{dep}', which was never scheduled",
                        f"task:{task.id}",
                    )
                )
                continue
            if starts[task.id] < ends[dep]:
                issues.append(
                    error(
                        "dependency_violation",
                        f"task '{task.id}' starts {fmt_dt(starts[task.id])} but depends on "
                        f"'{dep}', which does not finish until {fmt_dt(ends[dep])}",
                        f"task:{task.id}",
                    )
                )


def _check_capacity(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """No resource may run more concurrent work than its capacity allows.

    Uses a sweep line over start/end events. Ends are processed before starts at
    the same instant, so back-to-back blocks are not treated as an overlap.
    """
    by_resource: Dict[str, List[Assignment]] = defaultdict(list)
    for assignment in schedule.assignments:
        by_resource[assignment.resource_id].append(assignment)

    for resource_id, assignments in by_resource.items():
        resource = plan.resource(resource_id)
        if resource is None:
            continue  # already reported
        capacity = resource.capacity

        events: List[Tuple[datetime, int, str]] = []
        for assignment in assignments:
            events.append((assignment.start, 1, assignment.task_id))
            events.append((assignment.end, -1, assignment.task_id))
        events.sort(key=lambda e: (e[0], e[1]))

        active: List[str] = []
        reported: set = set()
        for when, delta, task_id in events:
            if delta == 1:
                active.append(task_id)
                if len(active) > capacity:
                    key = tuple(sorted(active))
                    if key not in reported:
                        reported.add(key)
                        issues.append(
                            error(
                                "over_capacity",
                                f"resource '{resource_id}' has capacity {capacity} but is "
                                f"running {len(active)} tasks at {fmt_dt(when)}: "
                                f"{', '.join(sorted(active))}",
                                f"resource:{resource_id}",
                            )
                        )
            else:
                if task_id in active:
                    active.remove(task_id)


def _check_availability(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Work must fall inside a resource's declared availability windows."""
    for assignment in schedule.assignments:
        resource = plan.resource(assignment.resource_id)
        if resource is None or not resource.available:
            continue
        if not resource.is_available_for(assignment.window):
            windows = ", ".join(
                f"{fmt_dt(w.start)}-{fmt_dt(w.end)}" for w in resource.available
            )
            issues.append(
                error(
                    "outside_availability",
                    f"task '{assignment.task_id}' runs {fmt_dt(assignment.start)} to "
                    f"{fmt_dt(assignment.end)}, outside availability for "
                    f"'{resource.id}' ({windows})",
                    _where(assignment),
                )
            )


def _check_granularity(plan: Plan, schedule: Schedule, issues: List[Issue]) -> None:
    """Soft check: start times should land on the plan's time grid."""
    step = plan.granularity_minutes
    if step <= 1:
        return
    origin = plan.horizon.start
    for assignment in schedule.assignments:
        offset = int((assignment.start - origin).total_seconds() // 60)
        if offset % step != 0:
            issues.append(
                warn(
                    "off_grid",
                    f"task '{assignment.task_id}' starts {fmt_dt(assignment.start)}, which is "
                    f"not on the {step}-minute grid",
                    _where(assignment),
                )
            )


# --------------------------------------------------------------------------
# Summary metrics
# --------------------------------------------------------------------------

def summarize(plan: Plan, schedule: Schedule) -> Dict[str, object]:
    """Numbers worth logging for every run."""
    # Count only tasks that actually exist in the plan, so a hallucinated task
    # id cannot inflate the count to "4 of 3".
    known_ids = set(plan.task_ids)
    scheduled_ids = {a.task_id for a in schedule.assignments} & known_ids
    utilization: Dict[str, int] = defaultdict(int)
    for assignment in schedule.assignments:
        utilization[assignment.resource_id] += assignment.window.minutes

    horizon_minutes = plan.horizon.minutes
    per_resource = {}
    for resource in plan.resources:
        used = utilization.get(resource.id, 0)
        capacity_minutes = horizon_minutes * resource.capacity
        pct = round(100.0 * used / capacity_minutes, 1) if capacity_minutes else 0.0
        per_resource[resource.id] = {"busy_minutes": used, "utilization_pct": pct}

    finish = max((a.end for a in schedule.assignments), default=None)

    return {
        "tasks_total": len(plan.tasks),
        "tasks_scheduled": len(scheduled_ids),
        "tasks_unscheduled": len(plan.tasks) - len(scheduled_ids),
        "makespan_minutes": schedule.makespan_minutes,
        "finish_time": fmt_dt(finish) if finish else None,
        "horizon_minutes": horizon_minutes,
        "resource_utilization": per_resource,
    }
