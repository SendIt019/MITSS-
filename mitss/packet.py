"""Build the prompt packet handed to the language model.

The packet is a single self-contained file: the plan, the output contract, and
the rules the answer will be checked against. Nothing else from the session is
needed to produce a valid schedule, which is what makes runs reproducible.
"""

from __future__ import annotations

import json

from .model import Plan, fmt_dt

OUTPUT_CONTRACT = """{
  "session": "<same session id as the plan>",
  "assignments": [
    {
      "task_id": "<task id from the plan>",
      "resource_id": "<resource id from the plan>",
      "start": "YYYY-MM-DDTHH:MM:SS",
      "end": "YYYY-MM-DDTHH:MM:SS"
    }
  ],
  "unscheduled": [
    {"task_id": "<task id>", "reason": "<why it could not be placed>"}
  ],
  "rationale": "<short explanation of the approach taken>"
}"""


def build_packet(plan: Plan) -> str:
    """Return the markdown packet to paste into the model."""
    rules = [
        "Return exactly one JSON object, inside a single ```json fenced code block. "
        "No prose outside the block.",
        "Schedule every task exactly once. If a task genuinely cannot be placed, put it "
        "in `unscheduled` with a reason instead of silently dropping it.",
        "Each assignment must last exactly the task's `duration_minutes`.",
        "A task may not start until every task in its `depends_on` list has ended.",
        "A resource may run at most `capacity` tasks at the same instant. Back-to-back "
        "blocks that merely touch endpoints are fine.",
        "If a task lists `requires`, assign it only to a resource in that list. An empty "
        "`requires` means any resource is eligible.",
        "If a resource lists `available` windows, all its work must fit inside them. An "
        "empty list means available for the whole horizon.",
        "Respect each task's `earliest_start` and `deadline` when present.",
        "Keep everything inside the plan horizon.",
        f"Start times should land on the {plan.granularity_minutes}-minute grid measured "
        f"from the horizon start ({fmt_dt(plan.horizon.start)}).",
        "Use the timestamp format YYYY-MM-DDTHH:MM:SS with no timezone suffix.",
    ]

    objectives = plan.objectives or ["produce a feasible schedule"]

    lines = [
        f"# MITSS scheduling request - session `{plan.session}`",
        "",
        f"Domain: **{plan.domain}**  |  Horizon: **{fmt_dt(plan.horizon.start)}** to "
        f"**{fmt_dt(plan.horizon.end)}**  |  Grid: **{plan.granularity_minutes} min**",
        "",
        "## Objectives, in priority order",
        "",
    ]
    for index, objective in enumerate(objectives, start=1):
        lines.append(f"{index}. {objective}")

    if plan.notes:
        lines += ["", "## Operator notes", "", plan.notes]

    lines += [
        "",
        "## Rules your answer is checked against",
        "",
    ]
    for rule in rules:
        lines.append(f"- {rule}")

    lines += [
        "",
        "## Plan",
        "",
        "```json",
        json.dumps(plan.to_dict(), indent=2),
        "```",
        "",
        "## Required output shape",
        "",
        "```json",
        OUTPUT_CONTRACT,
        "```",
        "",
        "Return only that JSON object, in one fenced block.",
        "",
    ]
    return "\n".join(lines)
