"""Structural validation: does the data have the right shape and types?

Domain rules (no double-booking, dependency order, and so on) live in
constraints.py. This module only answers "can I trust the shape of this?".

Every function returns (parsed_object_or_None, issues). Nothing raises on bad
input data; a malformed plan or schedule comes back as a list of errors.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .issues import Issue, error, has_errors, warn
from .model import Assignment, Interval, Plan, Resource, Schedule, Task, parse_dt


def _require_dict(data: Any, where: str, issues: List[Issue]) -> bool:
    if not isinstance(data, dict):
        issues.append(
            error("not_an_object", f"expected a JSON object, got {type(data).__name__}", where)
        )
        return False
    return True


def _get_str(data: Dict[str, Any], key: str, where: str, issues: List[Issue],
             default: str = "", required: bool = False) -> str:
    value = data.get(key, None)
    if value is None:
        if required:
            issues.append(error("missing_field", f"'{key}' is required", where))
        return default
    if not isinstance(value, str):
        issues.append(error("bad_type", f"'{key}' must be a string", where))
        return default
    return value


def _get_int(data: Dict[str, Any], key: str, where: str, issues: List[Issue],
             default: int = 0, minimum: Optional[int] = None,
             required: bool = False) -> int:
    value = data.get(key, None)
    if value is None:
        if required:
            issues.append(error("missing_field", f"'{key}' is required", where))
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(error("bad_type", f"'{key}' must be a whole number", where))
        return default
    if minimum is not None and value < minimum:
        issues.append(
            error("out_of_range", f"'{key}' must be at least {minimum}, got {value}", where)
        )
        return default
    return value


def _get_str_list(data: Dict[str, Any], key: str, where: str,
                  issues: List[Issue]) -> List[str]:
    value = data.get(key, None)
    if value is None:
        return []
    if not isinstance(value, list):
        issues.append(error("bad_type", f"'{key}' must be a list of strings", where))
        return []
    out: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(
                error("bad_type", f"'{key}[{index}]' must be a string", where)
            )
            continue
        out.append(item)
    return out


def _get_dt(data: Dict[str, Any], key: str, where: str, issues: List[Issue],
            required: bool = False):
    value = data.get(key, None)
    if value is None:
        if required:
            issues.append(error("missing_field", f"'{key}' is required", where))
        return None
    try:
        return parse_dt(value)
    except ValueError as exc:
        issues.append(error("bad_timestamp", f"'{key}': {exc}", where))
        return None


def _get_interval(data: Dict[str, Any], where: str,
                  issues: List[Issue]) -> Optional[Interval]:
    if not _require_dict(data, where, issues):
        return None
    start = _get_dt(data, "start", where, issues, required=True)
    end = _get_dt(data, "end", where, issues, required=True)
    if start is None or end is None:
        return None
    if end <= start:
        issues.append(
            error("bad_interval", f"end ({end}) must be after start ({start})", where)
        )
        return None
    return Interval(start, end)


# --------------------------------------------------------------------------
# Plan (input side)
# --------------------------------------------------------------------------

def validate_plan(data: Any) -> Tuple[Optional[Plan], List[Issue]]:
    issues: List[Issue] = []
    if not _require_dict(data, "plan", issues):
        return None, issues

    session = _get_str(data, "session", "plan", issues, required=True)
    domain = _get_str(data, "domain", "plan", issues, default="generic")
    granularity = _get_int(
        data, "granularity_minutes", "plan", issues, default=15, minimum=1
    )
    objectives = _get_str_list(data, "objectives", "plan", issues)
    notes = _get_str(data, "notes", "plan", issues)

    horizon_raw = data.get("horizon")
    if horizon_raw is None:
        issues.append(error("missing_field", "'horizon' is required", "plan"))
        horizon = None
    else:
        horizon = _get_interval(horizon_raw, "plan.horizon", issues)

    resources = _validate_resources(data.get("resources"), issues)
    tasks = _validate_tasks(data.get("tasks"), issues)

    _cross_check_plan(tasks, resources, horizon, issues)

    if has_errors(issues) or horizon is None:
        return None, issues

    plan = Plan(
        session=session,
        horizon=horizon,
        resources=resources,
        tasks=tasks,
        domain=domain,
        granularity_minutes=granularity,
        objectives=objectives,
        notes=notes,
    )
    return plan, issues


def _validate_resources(raw: Any, issues: List[Issue]) -> List[Resource]:
    if raw is None:
        issues.append(error("missing_field", "'resources' is required", "plan"))
        return []
    if not isinstance(raw, list):
        issues.append(error("bad_type", "'resources' must be a list", "plan"))
        return []
    if not raw:
        issues.append(error("empty_resources", "at least one resource is required", "plan"))
        return []

    resources: List[Resource] = []
    seen: set = set()
    for index, item in enumerate(raw):
        where = f"plan.resources[{index}]"
        if not _require_dict(item, where, issues):
            continue
        rid = _get_str(item, "id", where, issues, required=True)
        if not rid:
            continue
        if rid in seen:
            issues.append(error("duplicate_id", f"resource id '{rid}' appears more than once", where))
            continue
        seen.add(rid)
        name = _get_str(item, "name", where, issues, default=rid)
        capacity = _get_int(item, "capacity", where, issues, default=1, minimum=1)
        available: List[Interval] = []
        raw_windows = item.get("available")
        if raw_windows is not None:
            if not isinstance(raw_windows, list):
                issues.append(error("bad_type", "'available' must be a list of intervals", where))
            else:
                for w_index, window in enumerate(raw_windows):
                    parsed = _get_interval(window, f"{where}.available[{w_index}]", issues)
                    if parsed is not None:
                        available.append(parsed)
        resources.append(
            Resource(id=rid, name=name or rid, capacity=capacity, available=available)
        )
    return resources


def _validate_tasks(raw: Any, issues: List[Issue]) -> List[Task]:
    if raw is None:
        issues.append(error("missing_field", "'tasks' is required", "plan"))
        return []
    if not isinstance(raw, list):
        issues.append(error("bad_type", "'tasks' must be a list", "plan"))
        return []
    if not raw:
        issues.append(error("empty_tasks", "at least one task is required", "plan"))
        return []

    tasks: List[Task] = []
    seen: set = set()
    for index, item in enumerate(raw):
        where = f"plan.tasks[{index}]"
        if not _require_dict(item, where, issues):
            continue
        tid = _get_str(item, "id", where, issues, required=True)
        if not tid:
            continue
        if tid in seen:
            issues.append(error("duplicate_id", f"task id '{tid}' appears more than once", where))
            continue
        seen.add(tid)
        task = Task(
            id=tid,
            name=_get_str(item, "name", where, issues, default=tid) or tid,
            duration_minutes=_get_int(
                item, "duration_minutes", where, issues, default=0, minimum=1, required=True
            ),
            depends_on=_get_str_list(item, "depends_on", where, issues),
            requires=_get_str_list(item, "requires", where, issues),
            earliest_start=_get_dt(item, "earliest_start", where, issues),
            deadline=_get_dt(item, "deadline", where, issues),
            priority=_get_int(item, "priority", where, issues, default=3, minimum=1),
            notes=_get_str(item, "notes", where, issues),
        )
        if task.earliest_start and task.deadline and task.deadline <= task.earliest_start:
            issues.append(
                error(
                    "impossible_window",
                    f"deadline is at or before earliest_start for task '{tid}'",
                    where,
                )
            )
        tasks.append(task)
    return tasks


def _cross_check_plan(tasks: List[Task], resources: List[Resource],
                      horizon: Optional[Interval], issues: List[Issue]) -> None:
    """Catch plans that are unsatisfiable before we ever ask the model."""
    task_ids = {t.id for t in tasks}
    resource_ids = {r.id for r in resources}

    for task in tasks:
        where = f"plan.tasks[{task.id}]"
        for dep in task.depends_on:
            if dep == task.id:
                issues.append(error("self_dependency", f"task '{task.id}' depends on itself", where))
            elif dep not in task_ids:
                issues.append(
                    error("unknown_dependency", f"depends_on references unknown task '{dep}'", where)
                )
        for req in task.requires:
            if req not in resource_ids:
                issues.append(
                    error("unknown_resource", f"requires references unknown resource '{req}'", where)
                )
        if horizon is not None:
            if task.duration_minutes > horizon.minutes:
                issues.append(
                    error(
                        "task_exceeds_horizon",
                        f"task '{task.id}' needs {task.duration_minutes} min but the horizon is "
                        f"only {horizon.minutes} min",
                        where,
                    )
                )
            if task.deadline and task.deadline > horizon.end:
                issues.append(
                    warn(
                        "deadline_past_horizon",
                        f"task '{task.id}' has a deadline after the horizon ends",
                        where,
                    )
                )
            if task.earliest_start and task.earliest_start < horizon.start:
                issues.append(
                    warn(
                        "earliest_start_before_horizon",
                        f"task '{task.id}' may start before the horizon begins",
                        where,
                    )
                )

    cycle = find_dependency_cycle(tasks)
    if cycle:
        issues.append(
            error(
                "dependency_cycle",
                "dependencies form a loop that can never be satisfied: "
                + " -> ".join(cycle),
                "plan.tasks",
            )
        )


def find_dependency_cycle(tasks: List[Task]) -> Optional[List[str]]:
    """Return one cycle as a list of task ids, or None if the graph is acyclic."""
    graph = {t.id: [d for d in t.depends_on if d != t.id] for t in tasks}
    known = set(graph)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in graph}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        color[node] = GREY
        stack.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in known:
                continue
            if color[neighbor] == GREY:
                start = stack.index(neighbor)
                return stack[start:] + [neighbor]
            if color[neighbor] == WHITE:
                found = visit(neighbor)
                if found:
                    return found
        color[node] = BLACK
        stack.pop()
        return None

    for tid in graph:
        if color[tid] == WHITE:
            found = visit(tid)
            if found:
                return found
    return None


# --------------------------------------------------------------------------
# Schedule (output side)
# --------------------------------------------------------------------------

def validate_schedule(data: Any, plan: Optional[Plan] = None
                      ) -> Tuple[Optional[Schedule], List[Issue]]:
    issues: List[Issue] = []
    if not _require_dict(data, "schedule", issues):
        return None, issues

    session = _get_str(data, "session", "schedule", issues)
    if plan is not None and session and session != plan.session:
        issues.append(
            warn(
                "session_mismatch",
                f"output says session '{session}' but the plan is '{plan.session}'",
                "schedule",
            )
        )

    raw_assignments = data.get("assignments")
    assignments: List[Assignment] = []
    if raw_assignments is None:
        issues.append(error("missing_field", "'assignments' is required", "schedule"))
    elif not isinstance(raw_assignments, list):
        issues.append(error("bad_type", "'assignments' must be a list", "schedule"))
    else:
        for index, item in enumerate(raw_assignments):
            where = f"schedule.assignments[{index}]"
            if not _require_dict(item, where, issues):
                continue
            task_id = _get_str(item, "task_id", where, issues, required=True)
            resource_id = _get_str(item, "resource_id", where, issues, required=True)
            start = _get_dt(item, "start", where, issues, required=True)
            end = _get_dt(item, "end", where, issues, required=True)
            if not task_id or not resource_id or start is None or end is None:
                continue
            if end <= start:
                issues.append(
                    error("bad_interval", "assignment end must be after start", where)
                )
                continue
            assignments.append(Assignment(task_id, resource_id, start, end))

    unscheduled: List[Dict[str, str]] = []
    raw_unscheduled = data.get("unscheduled")
    if raw_unscheduled is not None:
        if not isinstance(raw_unscheduled, list):
            issues.append(error("bad_type", "'unscheduled' must be a list", "schedule"))
        else:
            for index, item in enumerate(raw_unscheduled):
                where = f"schedule.unscheduled[{index}]"
                if not _require_dict(item, where, issues):
                    continue
                task_id = _get_str(item, "task_id", where, issues, required=True)
                reason = _get_str(item, "reason", where, issues)
                if task_id:
                    unscheduled.append({"task_id": task_id, "reason": reason})

    rationale = _get_str(data, "rationale", "schedule", issues)

    if has_errors(issues):
        return None, issues

    schedule = Schedule(
        session=session or (plan.session if plan else ""),
        assignments=assignments,
        unscheduled=unscheduled,
        rationale=rationale,
    )
    return schedule, issues
