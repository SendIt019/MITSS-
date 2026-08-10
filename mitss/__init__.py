"""MITSS - an input/output harness for language-model scheduling runs.

The harness does not call any model. You paste the generated packet to the
model and paste its reply back; MITSS handles staging, parsing, validation,
constraint checking, rendering, diffing, and logging around that exchange.

No third-party dependencies, no network calls, no credentials.
"""

from .constraints import check_constraints, summarize
from .capture import extract_json
from .diffing import diff_schedules, render_diff
from .issues import Issue, format_issues, has_errors
from .model import Assignment, Interval, Plan, Resource, Schedule, Task
from .packet import build_packet
from .render import render_csv, render_summary, render_table, render_timeline
from .runlog import RunStore
from .validate import validate_plan, validate_schedule

__version__ = "0.1.0"

__all__ = [
    "Assignment",
    "Interval",
    "Issue",
    "Plan",
    "Resource",
    "RunStore",
    "Schedule",
    "Task",
    "build_packet",
    "check_constraints",
    "diff_schedules",
    "extract_json",
    "format_issues",
    "has_errors",
    "render_csv",
    "render_diff",
    "render_summary",
    "render_table",
    "render_timeline",
    "summarize",
    "validate_plan",
    "validate_schedule",
]
