"""Parse a plain-text plan file into the plan structure.

This is the deterministic path: a small line-oriented grammar that Python
understands without any model involved, so a malformed file produces exact
line-numbered errors instead of a guess.

Grammar (keywords are case-insensitive, `#` starts a comment, blank lines are
ignored, fields are separated by `|`):

    SESSION:   demo-001
    DOMAIN:    field-ops
    HORIZON:   2026-08-11 08:00 -> 18:00
    GRID:      15min
    OBJECTIVE: finish as early as possible
    NOTE:      any free text, repeatable

    RESOURCE: alpha | Team Alpha | cap 1
    RESOURCE: bravo | Team Bravo | cap 2 | available 12:00 -> 18:00

    TASK: t1 | Site survey     | 120min
    TASK: t2 | Equipment setup | 90min  | after t1
    TASK: t3 | Calibration     | 1h     | after t2 | needs alpha | by 16:00
    TASK: t4 | Teardown        | 45min  | after t3 | not before 14:00 | priority 1

Task and resource modifiers, in any order after the duration:

    after A, B      dependencies
    needs A, B      eligible resources
    by <time>       deadline
    not before <t>  earliest start
    priority N      lower number is more important
    cap N           resource capacity (resources only)
    available X->Y  resource availability window, repeatable

Times may be given as `HH:MM` (the horizon's start date is assumed) or as a
full `YYYY-MM-DD HH:MM`. Durations accept `90`, `90min`, `2h`, `1h30m`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .issues import Issue, error, warn

DIRECTIVES = {
    "session", "domain", "horizon", "grid", "objective", "note",
    "resource", "task",
}

_DURATION = re.compile(
    r"^\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*$",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"^\s*(\d+)\s*$")
_ARROW = re.compile(r"\s*(?:->|-->|to)\s*", re.IGNORECASE)


def looks_structured(text: str) -> bool:
    """True if the file uses the grammar at all.

    Used to decide whether to fall back to letting a model structure the text.
    A file needs a horizon plus at least one task line to count.
    """
    seen = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        keyword = line.split(":", 1)[0].strip().lower()
        if keyword in DIRECTIVES:
            seen.add(keyword)
    return "task" in seen and "horizon" in seen


def parse_text_plan(text: str) -> Tuple[Optional[Dict[str, Any]], List[Issue]]:
    """Parse the grammar into a plan dict. Returns (plan_dict_or_None, issues).

    The returned dict is not validated here — hand it to validate_plan, which
    performs the type and cross-reference checks.
    """
    issues: List[Issue] = []
    session = ""
    domain = "generic"
    grid = 15
    objectives: List[str] = []
    notes: List[str] = []
    resources: List[Dict[str, Any]] = []
    tasks: List[Dict[str, Any]] = []
    horizon: Optional[Dict[str, str]] = None
    base_date: Optional[datetime] = None

    # First pass: find the horizon so bare HH:MM times elsewhere have a date.
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip(raw)
        if not line:
            continue
        keyword, _, body = line.partition(":")
        if keyword.strip().lower() != "horizon":
            continue
        parsed, horizon_issues = _parse_horizon(body.strip(), number)
        issues.extend(horizon_issues)
        if parsed:
            horizon = {"start": parsed[0].strftime("%Y-%m-%dT%H:%M:%S"),
                       "end": parsed[1].strftime("%Y-%m-%dT%H:%M:%S")}
            base_date = parsed[0]
        break

    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip(raw)
        if not line:
            continue
        where = f"line {number}"

        if ":" not in line:
            issues.append(
                error("unknown_line", f"cannot read {line!r}; expected KEYWORD: value", where)
            )
            continue

        keyword, _, body = line.partition(":")
        keyword = keyword.strip().lower()
        body = body.strip()

        if keyword == "session":
            session = body
        elif keyword == "domain":
            domain = body or "generic"
        elif keyword == "horizon":
            continue  # handled in the first pass
        elif keyword == "grid":
            minutes = _parse_duration(body)
            if minutes is None:
                issues.append(error("bad_grid", f"cannot read grid {body!r}", where))
            else:
                grid = minutes
        elif keyword == "objective":
            if body:
                objectives.append(body)
        elif keyword == "note":
            if body:
                notes.append(body)
        elif keyword == "resource":
            resource, resource_issues = _parse_resource(body, number, base_date)
            issues.extend(resource_issues)
            if resource:
                resources.append(resource)
        elif keyword == "task":
            task, task_issues = _parse_task(body, number, base_date)
            issues.extend(task_issues)
            if task:
                tasks.append(task)
        else:
            issues.append(
                error(
                    "unknown_keyword",
                    f"'{keyword}' is not a known keyword "
                    f"(expected one of: {', '.join(sorted(DIRECTIVES))})",
                    where,
                )
            )

    if horizon is None:
        issues.append(
            error("missing_horizon", "no HORIZON line found; one is required", "file")
        )
    if not tasks:
        issues.append(error("no_tasks", "no TASK lines found", "file"))
    if not resources:
        issues.append(error("no_resources", "no RESOURCE lines found", "file"))

    if any(i.severity == "error" for i in issues):
        return None, issues

    plan = {
        "session": session or "untitled",
        "domain": domain,
        "horizon": horizon,
        "granularity_minutes": grid,
        "objectives": objectives,
        "notes": "\n".join(notes),
        "resources": resources,
        "tasks": tasks,
    }
    if not session:
        issues.append(
            warn("no_session", "no SESSION line; using 'untitled'", "file")
        )
    return plan, issues


# --------------------------------------------------------------------------
# line parsers
# --------------------------------------------------------------------------

def _strip(raw: str) -> str:
    """Remove comments and surrounding whitespace."""
    return raw.split("#", 1)[0].strip()


def _parse_horizon(body: str, number: int
                   ) -> Tuple[Optional[Tuple[datetime, datetime]], List[Issue]]:
    where = f"line {number}"
    parts = _ARROW.split(body, maxsplit=1)
    if len(parts) != 2:
        return None, [
            error(
                "bad_horizon",
                "expected 'HORIZON: <start> -> <end>'",
                where,
            )
        ]
    start = _parse_moment(parts[0].strip(), None)
    if start is None:
        return None, [error("bad_horizon", f"cannot read start time {parts[0]!r}", where)]
    end = _parse_moment(parts[1].strip(), start)
    if end is None:
        return None, [error("bad_horizon", f"cannot read end time {parts[1]!r}", where)]
    if end <= start:
        return None, [error("bad_horizon", "horizon end must be after its start", where)]
    return (start, end), []


def _parse_resource(body: str, number: int, base: Optional[datetime]
                    ) -> Tuple[Optional[Dict[str, Any]], List[Issue]]:
    where = f"line {number}"
    issues: List[Issue] = []
    fields = [f.strip() for f in body.split("|")]
    if not fields or not fields[0]:
        return None, [error("bad_resource", "RESOURCE needs an id", where)]

    resource: Dict[str, Any] = {
        "id": fields[0],
        "name": fields[0],
        "capacity": 1,
        "available": [],
    }

    for field in fields[1:]:
        if not field:
            continue
        lowered = field.lower()
        if lowered.startswith("cap"):
            match = re.search(r"(\d+)", field)
            if not match:
                issues.append(error("bad_capacity", f"cannot read capacity from {field!r}", where))
            else:
                resource["capacity"] = int(match.group(1))
        elif lowered.startswith("available"):
            window, window_issues = _parse_window(field.split(None, 1)[1] if " " in field else "",
                                                  number, base)
            issues.extend(window_issues)
            if window:
                resource["available"].append(window)
        elif resource["name"] == resource["id"]:
            resource["name"] = field
        else:
            issues.append(
                error("unknown_field", f"cannot read resource field {field!r}", where)
            )

    return resource, issues


def _parse_task(body: str, number: int, base: Optional[datetime]
                ) -> Tuple[Optional[Dict[str, Any]], List[Issue]]:
    where = f"line {number}"
    issues: List[Issue] = []
    fields = [f.strip() for f in body.split("|")]
    if not fields or not fields[0]:
        return None, [error("bad_task", "TASK needs an id", where)]

    task: Dict[str, Any] = {
        "id": fields[0],
        "name": fields[0],
        "duration_minutes": None,
        "depends_on": [],
        "requires": [],
        "priority": 3,
    }
    name_set = False

    for field in fields[1:]:
        if not field:
            continue
        lowered = field.lower()

        if lowered.startswith("after"):
            task["depends_on"].extend(_id_list(field))
        elif lowered.startswith("needs") or lowered.startswith("requires"):
            task["requires"].extend(_id_list(field))
        elif lowered.startswith("not before") or lowered.startswith("notbefore"):
            moment = _parse_moment(_after_keyword(field, 2), base)
            if moment is None:
                issues.append(error("bad_time", f"cannot read time in {field!r}", where))
            else:
                task["earliest_start"] = moment.strftime("%Y-%m-%dT%H:%M:%S")
        elif lowered.startswith("by ") or lowered == "by":
            moment = _parse_moment(_after_keyword(field, 1), base)
            if moment is None:
                issues.append(error("bad_time", f"cannot read deadline in {field!r}", where))
            else:
                task["deadline"] = moment.strftime("%Y-%m-%dT%H:%M:%S")
        elif lowered.startswith("priority"):
            match = re.search(r"(\d+)", field)
            if not match:
                issues.append(error("bad_priority", f"cannot read priority in {field!r}", where))
            else:
                task["priority"] = int(match.group(1))
        else:
            duration = _parse_duration(field)
            if duration is not None:
                if task["duration_minutes"] is not None:
                    issues.append(
                        error("duplicate_duration",
                              f"duration given more than once ({field!r})", where)
                    )
                task["duration_minutes"] = duration
            elif not name_set:
                task["name"] = field
                name_set = True
            else:
                issues.append(
                    error("unknown_field", f"cannot read task field {field!r}", where)
                )

    if task["duration_minutes"] is None:
        issues.append(
            error("missing_duration",
                  f"task '{task['id']}' has no duration (e.g. 90min, 2h, 1h30m)", where)
        )
        return None, issues

    return task, issues


def _parse_window(body: str, number: int, base: Optional[datetime]
                  ) -> Tuple[Optional[Dict[str, str]], List[Issue]]:
    where = f"line {number}"
    parts = _ARROW.split(body.strip(), maxsplit=1)
    if len(parts) != 2:
        return None, [error("bad_window", "expected 'available <start> -> <end>'", where)]
    start = _parse_moment(parts[0].strip(), base)
    end = _parse_moment(parts[1].strip(), base if start is None else start)
    if start is None or end is None:
        return None, [error("bad_window", f"cannot read window {body!r}", where)]
    if end <= start:
        return None, [error("bad_window", "window end must be after its start", where)]
    return {"start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S")}, []


# --------------------------------------------------------------------------
# scalar parsers
# --------------------------------------------------------------------------

def _after_keyword(field: str, words: int) -> str:
    """Drop the leading keyword words and return the remainder."""
    parts = field.split(None, words)
    return parts[words].strip() if len(parts) > words else ""


def _id_list(field: str) -> List[str]:
    """'after t1, t2 t3' -> ['t1', 't2', 't3']"""
    remainder = field.split(None, 1)[1] if " " in field else ""
    return [token for token in re.split(r"[,\s]+", remainder.strip()) if token]


def _parse_duration(text: str) -> Optional[int]:
    """Accept 90, 90min, 2h, 1h30m. Returns minutes, or None if unreadable."""
    if not text:
        return None
    bare = _BARE_NUMBER.match(text)
    if bare:
        return int(bare.group(1))
    match = _DURATION.match(text)
    if not match or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    mins = int(match.group(2) or 0)
    total = hours * 60 + mins
    return total or None


def _parse_moment(text: str, base: Optional[datetime]) -> Optional[datetime]:
    """Parse a full timestamp, or an HH:MM borrowing the base's date."""
    if not text:
        return None
    text = text.strip().replace("T", " ")

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if base is None:
            return None
        moment = base.replace(
            hour=clock.hour, minute=clock.minute, second=clock.second, microsecond=0
        )
        # A bare clock time that lands before the reference means the next day.
        if moment < base:
            moment += timedelta(days=1)
        return moment

    return None
