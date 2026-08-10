"""Tests for the plain-text plan grammar and the LLM fallback decision."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mitss.packet import build_structuring_packet
from mitss.textplan import looks_structured, parse_text_plan
from mitss.validate import validate_plan

GOOD = """
# Field exercise, first pass
SESSION: demo-001
DOMAIN: field-ops
HORIZON: 2026-08-11 08:00 -> 18:00
GRID: 15min
OBJECTIVE: finish as early as possible
NOTE: weather window closes at 1600

RESOURCE: alpha | Team Alpha | cap 1
RESOURCE: bravo | Team Bravo | cap 2 | available 12:00 -> 18:00

TASK: t1 | Site survey     | 120min
TASK: t2 | Equipment setup | 1h30m | after t1
TASK: t3 | Calibration     | 1h    | after t2 | needs alpha | by 16:00
TASK: t4 | Teardown        | 45    | after t3 | not before 14:00 | priority 1
"""


def codes(issues):
    return {i.code for i in issues}


class Grammar(unittest.TestCase):
    def test_full_file_parses_and_validates(self):
        plan_dict, issues = parse_text_plan(GOOD)
        self.assertIsNotNone(plan_dict, [str(i) for i in issues])
        plan, validation = validate_plan(plan_dict)
        self.assertIsNotNone(plan, [str(i) for i in validation])

        self.assertEqual(plan.session, "demo-001")
        self.assertEqual(plan.domain, "field-ops")
        self.assertEqual(plan.granularity_minutes, 15)
        self.assertEqual(len(plan.tasks), 4)
        self.assertEqual(len(plan.resources), 2)
        self.assertEqual(plan.objectives, ["finish as early as possible"])
        self.assertIn("weather window", plan.notes)

    def test_durations_in_several_notations(self):
        plan_dict, _ = parse_text_plan(GOOD)
        durations = {t["id"]: t["duration_minutes"] for t in plan_dict["tasks"]}
        self.assertEqual(durations["t1"], 120)   # 120min
        self.assertEqual(durations["t2"], 90)    # 1h30m
        self.assertEqual(durations["t3"], 60)    # 1h
        self.assertEqual(durations["t4"], 45)    # bare number

    def test_modifiers_are_captured(self):
        plan_dict, _ = parse_text_plan(GOOD)
        tasks = {t["id"]: t for t in plan_dict["tasks"]}
        self.assertEqual(tasks["t2"]["depends_on"], ["t1"])
        self.assertEqual(tasks["t3"]["requires"], ["alpha"])
        self.assertEqual(tasks["t3"]["deadline"], "2026-08-11T16:00:00")
        self.assertEqual(tasks["t4"]["earliest_start"], "2026-08-11T14:00:00")
        self.assertEqual(tasks["t4"]["priority"], 1)

    def test_resource_capacity_and_window(self):
        plan_dict, _ = parse_text_plan(GOOD)
        resources = {r["id"]: r for r in plan_dict["resources"]}
        self.assertEqual(resources["alpha"]["capacity"], 1)
        self.assertEqual(resources["bravo"]["capacity"], 2)
        self.assertEqual(resources["bravo"]["available"][0]["start"], "2026-08-11T12:00:00")
        self.assertEqual(resources["bravo"]["name"], "Team Bravo")

    def test_bare_clock_times_borrow_the_horizon_date(self):
        plan_dict, _ = parse_text_plan(GOOD)
        self.assertEqual(plan_dict["horizon"]["end"], "2026-08-11T18:00:00")

    def test_comments_and_blank_lines_ignored(self):
        text = GOOD.replace("SESSION: demo-001", "SESSION: demo-001   # trailing comment")
        plan_dict, issues = parse_text_plan(text)
        self.assertIsNotNone(plan_dict, [str(i) for i in issues])
        self.assertEqual(plan_dict["session"], "demo-001")

    def test_comma_and_space_separated_dependencies(self):
        text = """HORIZON: 2026-08-11 08:00 -> 18:00
RESOURCE: alpha
TASK: a | 30min
TASK: b | 30min
TASK: c | 30min | after a, b
"""
        plan_dict, issues = parse_text_plan(text)
        self.assertIsNotNone(plan_dict, [str(i) for i in issues])
        tasks = {t["id"]: t for t in plan_dict["tasks"]}
        self.assertEqual(sorted(tasks["c"]["depends_on"]), ["a", "b"])


class GrammarErrors(unittest.TestCase):
    def test_missing_duration_is_a_line_numbered_error(self):
        text = """HORIZON: 2026-08-11 08:00 -> 18:00
RESOURCE: alpha
TASK: t1 | Survey with no duration
"""
        plan_dict, issues = parse_text_plan(text)
        self.assertIsNone(plan_dict)
        self.assertIn("missing_duration", codes(issues))
        self.assertTrue(any(i.where == "line 3" for i in issues), [str(i) for i in issues])

    def test_unknown_keyword_reported(self):
        text = """HORIZON: 2026-08-11 08:00 -> 18:00
RESOURCE: alpha
TASK: t1 | 30min
WIDGET: nonsense
"""
        plan_dict, issues = parse_text_plan(text)
        self.assertIsNone(plan_dict)
        self.assertIn("unknown_keyword", codes(issues))

    def test_missing_horizon(self):
        plan_dict, issues = parse_text_plan("RESOURCE: alpha\nTASK: t1 | 30min\n")
        self.assertIsNone(plan_dict)
        self.assertIn("missing_horizon", codes(issues))

    def test_backwards_horizon(self):
        plan_dict, issues = parse_text_plan(
            "HORIZON: 2026-08-11 18:00 -> 2026-08-11 08:00\nRESOURCE: a\nTASK: t | 30min\n")
        self.assertIsNone(plan_dict)
        self.assertIn("bad_horizon", codes(issues))

    def test_no_tasks_or_resources(self):
        plan_dict, issues = parse_text_plan("HORIZON: 2026-08-11 08:00 -> 18:00\n")
        self.assertIsNone(plan_dict)
        self.assertIn("no_tasks", codes(issues))
        self.assertIn("no_resources", codes(issues))


class Fallback(unittest.TestCase):
    def test_structured_file_is_recognised(self):
        self.assertTrue(looks_structured(GOOD))

    def test_prose_is_not_structured(self):
        prose = (
            "We need to survey the north site, which takes about two hours, then "
            "set up equipment and calibrate before the weather turns at four."
        )
        self.assertFalse(looks_structured(prose))

    def test_partial_file_without_tasks_is_not_structured(self):
        self.assertFalse(looks_structured("HORIZON: 2026-08-11 08:00 -> 18:00\n"))

    def test_structuring_packet_carries_text_and_contract(self):
        prose = "Survey the site, then set up, then calibrate."
        packet = build_structuring_packet(prose, ["ERROR no_tasks: no TASK lines found"])
        self.assertIn(prose, packet)
        self.assertIn("```json", packet)
        self.assertIn("duration_minutes", packet)
        self.assertIn("no TASK lines found", packet)
        self.assertIn("Invent nothing", packet)


if __name__ == "__main__":
    unittest.main(verbosity=2)
