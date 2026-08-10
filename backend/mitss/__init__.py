"""MITSS - an input/output harness for language-model scheduling runs.

The core package calls no model by default and has no third-party
dependencies. You hand the generated packet to whatever model you use and
paste the reply back; MITSS handles parsing, validation, constraint checking,
rendering, diffing, and logging around that exchange.

A custom model can be plugged in through `llm.LLMProvider` — see that module.
Credentials are read from the environment at call time and never stored.
"""

from .constraints import check_constraints, summarize
from .capture import extract_json
from .diffing import diff_schedules, render_diff
from .issues import Issue, format_issues, has_errors
from .llm import (
    LLMError,
    LLMProvider,
    ProviderUnavailable,
    available_providers,
    get_provider,
    register_provider,
)
from .model import Assignment, Interval, Plan, Resource, Schedule, Task
from .packet import build_packet, build_structuring_packet
from .render import render_csv, render_summary, render_table, render_timeline
from .runlog import RunStore
from .textplan import looks_structured, parse_text_plan
from .validate import validate_plan, validate_schedule

__version__ = "0.2.0"

__all__ = [
    "Assignment",
    "Interval",
    "Issue",
    "LLMError",
    "LLMProvider",
    "Plan",
    "ProviderUnavailable",
    "Resource",
    "RunStore",
    "Schedule",
    "Task",
    "available_providers",
    "build_packet",
    "build_structuring_packet",
    "check_constraints",
    "diff_schedules",
    "extract_json",
    "format_issues",
    "get_provider",
    "has_errors",
    "looks_structured",
    "parse_text_plan",
    "register_provider",
    "render_csv",
    "render_diff",
    "render_summary",
    "render_table",
    "render_timeline",
    "summarize",
    "validate_plan",
    "validate_schedule",
]
