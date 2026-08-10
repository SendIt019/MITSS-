"""Seed the scheduling example into the prompt library.

The scheduling code in `mitss/` is a worked example of a domain that needs a
carefully specified prompt: it builds a packet stating a plan and every rule
the answer will be checked against. That packet is exactly the kind of thing
the pipeline is for, so this script drops it in as a starting prompt.

    cd backend && python -m examples.seed_scheduling

Note the division of labour. The pipeline does not know or care that this
prompt is about scheduling — it is text in, text out. The scheduling package
happens to also ship a validator, which you can run separately on an output you
captured. Most prompts will not have one, which is why the pipeline never
assumes there is.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mitss.packet import build_packet
from mitss.textplan import parse_text_plan
from mitss.validate import validate_plan
from pipeline import Store

EXAMPLE_PLAN = """SESSION: demo-001
DOMAIN: field-ops
HORIZON: 2026-08-11 08:00 -> 18:00
GRID: 15min
OBJECTIVE: finish as early as possible

RESOURCE: alpha | Team Alpha | cap 1
RESOURCE: bravo | Team Bravo | cap 2

TASK: t1 | Site survey     | 120min
TASK: t2 | Equipment setup | 1h30m | after t1
TASK: t3 | Calibration     | 1h    | after t2 | needs alpha | by 16:00
TASK: t4 | Teardown        | 45min | after t3
"""

NAME = "Scheduling (worked example)"


def build_prompt_text() -> str:
    plan_dict, issues = parse_text_plan(EXAMPLE_PLAN)
    if plan_dict is None:
        raise SystemExit("the example plan no longer parses: "
                         + "; ".join(str(i) for i in issues))
    plan, validation = validate_plan(plan_dict)
    if plan is None:
        raise SystemExit("the example plan no longer validates: "
                         + "; ".join(str(i) for i in validation))
    return build_packet(plan)


def main() -> int:
    root = os.environ.get("MITSS_ROOT", os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    store = Store(root)

    for existing in store.list_prompts():
        if existing.name == NAME:
            print(f"already seeded as '{existing.id}' — nothing to do")
            return 0

    prompt = store.create_prompt(
        NAME,
        build_prompt_text(),
        "generated from mitss.packet.build_packet",
    )
    print(f"created prompt '{prompt.id}' with {len(prompt.latest.text)} characters")
    print("open the interface and it will be in the list on the left")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
