"""Unit tests for the MITSS harness. Run with: python -m unittest discover tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mitss.capture import extract_json
from mitss.constraints import check_constraints, summarize
from mitss.diffing import diff_schedules
from mitss.issues import has_errors
from mitss.model import fmt_dt
from mitss.packet import build_packet
from mitss.render import render_csv, render_table, render_timeline
from mitss.runlog import RunStore
from mitss.validate import validate_plan, validate_schedule


def base_plan(**overrides):
    plan = {
        "session": "t-001",
        "horizon": {"start": "2026-08-11T08:00:00", "end": "2026-08-11T18:00:00"},
        "granularity_minutes": 30,
        "resources": [
            {"id": "alpha", "capacity": 1},
            {"id": "bravo", "capacity": 2},
        ],
        "tasks": [
            {"id": "t1", "duration_minutes": 60},
            {"id": "t2", "duration_minutes": 60, "depends_on": ["t1"]},
        ],
    }
    plan.update(overrides)
    return plan


def codes(issues):
    return {i.code for i in issues}


def _chart_rows(timeline: str) -> str:
    """Just the bar rows of a timeline, excluding the explanatory footer."""
    return "\n".join(line for line in timeline.splitlines() if "|" in line)


class PlanValidation(unittest.TestCase):
    def test_good_plan_parses(self):
        plan, issues = validate_plan(base_plan())
        self.assertIsNotNone(plan)
        self.assertFalse(has_errors(issues))
        self.assertEqual(len(plan.tasks), 2)
        self.assertEqual(plan.resource("bravo").capacity, 2)

    def test_missing_required_fields_reported_together(self):
        plan, issues = validate_plan({"session": "x"})
        self.assertIsNone(plan)
        self.assertIn("missing_field", codes(issues))
        # horizon, resources and tasks should all be flagged in one pass
        self.assertGreaterEqual(len([i for i in issues if i.code == "missing_field"]), 3)

    def test_unknown_dependency_and_resource(self):
        data = base_plan(
            tasks=[{"id": "t1", "duration_minutes": 60,
                    "depends_on": ["ghost"], "requires": ["nowhere"]}]
        )
        plan, issues = validate_plan(data)
        self.assertIsNone(plan)
        self.assertIn("unknown_dependency", codes(issues))
        self.assertIn("unknown_resource", codes(issues))

    def test_dependency_cycle_detected(self):
        data = base_plan(
            tasks=[
                {"id": "a", "duration_minutes": 30, "depends_on": ["c"]},
                {"id": "b", "duration_minutes": 30, "depends_on": ["a"]},
                {"id": "c", "duration_minutes": 30, "depends_on": ["b"]},
            ]
        )
        plan, issues = validate_plan(data)
        self.assertIsNone(plan)
        self.assertIn("dependency_cycle", codes(issues))

    def test_task_longer_than_horizon_rejected(self):
        data = base_plan(tasks=[{"id": "t1", "duration_minutes": 5000}])
        plan, issues = validate_plan(data)
        self.assertIsNone(plan)
        self.assertIn("task_exceeds_horizon", codes(issues))

    def test_duplicate_ids_rejected(self):
        data = base_plan(
            tasks=[{"id": "t1", "duration_minutes": 30}, {"id": "t1", "duration_minutes": 30}]
        )
        plan, issues = validate_plan(data)
        self.assertIsNone(plan)
        self.assertIn("duplicate_id", codes(issues))

    def test_bad_timestamp_reported(self):
        data = base_plan(horizon={"start": "not-a-date", "end": "2026-08-11T18:00:00"})
        plan, issues = validate_plan(data)
        self.assertIsNone(plan)
        self.assertIn("bad_timestamp", codes(issues))


class ScheduleValidation(unittest.TestCase):
    def setUp(self):
        self.plan, _ = validate_plan(base_plan())

    def test_valid_schedule(self):
        data = {
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
                {"task_id": "t2", "resource_id": "alpha",
                 "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
            ],
        }
        schedule, issues = validate_schedule(data, self.plan)
        self.assertIsNotNone(schedule)
        self.assertFalse(has_errors(issues + check_constraints(self.plan, schedule)))

    def test_end_before_start_rejected(self):
        data = {
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T10:00:00", "end": "2026-08-11T09:00:00"},
            ],
        }
        schedule, issues = validate_schedule(data, self.plan)
        self.assertIsNone(schedule)
        self.assertIn("bad_interval", codes(issues))

    def test_missing_assignments_key(self):
        schedule, issues = validate_schedule({"session": "t-001"}, self.plan)
        self.assertIsNone(schedule)
        self.assertIn("missing_field", codes(issues))


class Constraints(unittest.TestCase):
    def setUp(self):
        self.plan, _ = validate_plan(base_plan())

    def _check(self, assignments, unscheduled=None):
        data = {"session": "t-001", "assignments": assignments,
                "unscheduled": unscheduled or []}
        schedule, issues = validate_schedule(data, self.plan)
        self.assertIsNotNone(schedule, f"schema errors: {issues}")
        return check_constraints(self.plan, schedule)

    def test_dependency_violation(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
            {"task_id": "t2", "resource_id": "bravo",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
        ])
        self.assertIn("dependency_violation", codes(issues))

    def test_duration_mismatch(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T08:30:00"},
        ])
        self.assertIn("duration_mismatch", codes(issues))

    def test_over_capacity_on_single_capacity_resource(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
            {"task_id": "t2", "resource_id": "alpha",
             "start": "2026-08-11T08:30:00", "end": "2026-08-11T09:30:00"},
        ])
        self.assertIn("over_capacity", codes(issues))

    def test_back_to_back_is_not_over_capacity(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
            {"task_id": "t2", "resource_id": "alpha",
             "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
        ])
        self.assertNotIn("over_capacity", codes(issues))

    def test_capacity_two_allows_concurrent_work(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "bravo",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
            {"task_id": "t2", "resource_id": "bravo",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
        ])
        self.assertNotIn("over_capacity", codes(issues))

    def test_missing_task_flagged(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
        ])
        self.assertIn("task_missing", codes(issues))

    def test_declared_unscheduled_is_not_an_error(self):
        issues = self._check(
            [{"task_id": "t1", "resource_id": "alpha",
              "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}],
            unscheduled=[{"task_id": "t2", "reason": "no window left"}],
        )
        self.assertNotIn("task_missing", codes(issues))
        self.assertFalse(has_errors(issues))

    def test_task_scheduled_twice(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
            {"task_id": "t1", "resource_id": "bravo",
             "start": "2026-08-11T10:00:00", "end": "2026-08-11T11:00:00"},
            {"task_id": "t2", "resource_id": "bravo",
             "start": "2026-08-11T11:00:00", "end": "2026-08-11T12:00:00"},
        ])
        self.assertIn("task_scheduled_twice", codes(issues))

    def test_outside_horizon(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T20:00:00", "end": "2026-08-11T21:00:00"},
        ])
        self.assertIn("outside_horizon", codes(issues))

    def test_unknown_ids_flagged(self):
        issues = self._check([
            {"task_id": "ghost", "resource_id": "nowhere",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
        ])
        self.assertIn("unknown_task", codes(issues))
        self.assertIn("unknown_resource", codes(issues))

    def test_deadline_and_earliest_start(self):
        data = base_plan(tasks=[
            {"id": "t1", "duration_minutes": 60,
             "earliest_start": "2026-08-11T12:00:00",
             "deadline": "2026-08-11T14:00:00"},
        ])
        plan, _ = validate_plan(data)
        early = {"session": "t-001", "assignments": [
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}]}
        schedule, _ = validate_schedule(early, plan)
        self.assertIn("before_earliest_start", codes(check_constraints(plan, schedule)))

        late = {"session": "t-001", "assignments": [
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T15:00:00", "end": "2026-08-11T16:00:00"}]}
        schedule, _ = validate_schedule(late, plan)
        self.assertIn("past_deadline", codes(check_constraints(plan, schedule)))

    def test_ineligible_resource(self):
        data = base_plan(tasks=[{"id": "t1", "duration_minutes": 60, "requires": ["alpha"]}])
        plan, _ = validate_plan(data)
        payload = {"session": "t-001", "assignments": [
            {"task_id": "t1", "resource_id": "bravo",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}]}
        schedule, _ = validate_schedule(payload, plan)
        self.assertIn("ineligible_resource", codes(check_constraints(plan, schedule)))

    def test_availability_window_enforced(self):
        data = base_plan(
            resources=[{"id": "alpha", "capacity": 1, "available": [
                {"start": "2026-08-11T13:00:00", "end": "2026-08-11T17:00:00"}]}],
            tasks=[{"id": "t1", "duration_minutes": 60}],
        )
        plan, _ = validate_plan(data)
        payload = {"session": "t-001", "assignments": [
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}]}
        schedule, _ = validate_schedule(payload, plan)
        self.assertIn("outside_availability", codes(check_constraints(plan, schedule)))

    def test_off_grid_is_a_warning_not_an_error(self):
        issues = self._check([
            {"task_id": "t1", "resource_id": "alpha",
             "start": "2026-08-11T08:07:00", "end": "2026-08-11T09:07:00"},
            {"task_id": "t2", "resource_id": "alpha",
             "start": "2026-08-11T09:07:00", "end": "2026-08-11T10:07:00"},
        ])
        self.assertIn("off_grid", codes(issues))
        self.assertFalse(has_errors(issues))


class Capture(unittest.TestCase):
    def test_fenced_block(self):
        text = 'Here you go:\n\n```json\n{"session": "x", "assignments": []}\n```\n\nHope that helps.'
        parsed, issues = extract_json(text)
        self.assertEqual(parsed["session"], "x")
        self.assertFalse(has_errors(issues))

    def test_bare_json(self):
        parsed, issues = extract_json('{"session": "x", "assignments": []}')
        self.assertEqual(parsed["session"], "x")

    def test_json_buried_in_prose(self):
        text = 'I think the best plan is {"session": "x", "assignments": []} overall.'
        parsed, _ = extract_json(text)
        self.assertEqual(parsed["session"], "x")

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        text = 'note {"session": "a}b", "assignments": []} end'
        parsed, _ = extract_json(text)
        self.assertEqual(parsed["session"], "a}b")

    def test_unparseable(self):
        parsed, issues = extract_json("no json here at all")
        self.assertIsNone(parsed)
        self.assertIn("unparseable_output", codes(issues))

    def test_empty(self):
        parsed, issues = extract_json("   ")
        self.assertIsNone(parsed)
        self.assertIn("empty_output", codes(issues))


class Diffing(unittest.TestCase):
    def setUp(self):
        self.plan, _ = validate_plan(base_plan())

    def _schedule(self, assignments):
        schedule, _ = validate_schedule(
            {"session": "t-001", "assignments": assignments}, self.plan)
        return schedule

    def test_identical_runs_have_no_changes(self):
        rows = [{"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}]
        self.assertEqual(diff_schedules(self._schedule(rows), self._schedule(rows)), [])

    def test_moved_and_reassigned_detected(self):
        before = self._schedule([{"task_id": "t1", "resource_id": "alpha",
                                  "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}])
        moved = self._schedule([{"task_id": "t1", "resource_id": "alpha",
                                 "start": "2026-08-11T10:00:00", "end": "2026-08-11T11:00:00"}])
        self.assertEqual(diff_schedules(before, moved)[0]["change"], "moved")

        reassigned = self._schedule([{"task_id": "t1", "resource_id": "bravo",
                                      "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}])
        self.assertEqual(diff_schedules(before, reassigned)[0]["change"], "reassigned")

    def test_added_and_removed(self):
        empty = self._schedule([])
        one = self._schedule([{"task_id": "t1", "resource_id": "alpha",
                               "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"}])
        self.assertEqual(diff_schedules(empty, one)[0]["change"], "added")
        self.assertEqual(diff_schedules(one, empty)[0]["change"], "removed")


class Rendering(unittest.TestCase):
    def setUp(self):
        self.plan, _ = validate_plan(base_plan())
        self.schedule, _ = validate_schedule({
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
                {"task_id": "t2", "resource_id": "bravo",
                 "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
            ],
        }, self.plan)

    def test_table_lists_every_assignment(self):
        table = render_table(self.plan, self.schedule)
        self.assertIn("t1", table)
        self.assertIn("t2", table)

    def test_csv_has_header_and_rows(self):
        rows = render_csv(self.plan, self.schedule).strip().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].startswith("task_id,"))

    def test_timeline_has_a_row_per_resource(self):
        timeline = render_timeline(self.plan, self.schedule)
        self.assertIn("alpha", timeline)
        self.assertIn("bravo", timeline)

    def test_timeline_does_not_fake_an_overlap_for_back_to_back_work(self):
        # Regression: coarse cells once made two sequential tasks on one
        # resource render as '!', implying a conflict the checker had passed.
        schedule, _ = validate_schedule({
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
                {"task_id": "t2", "resource_id": "alpha",
                 "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
            ],
        }, self.plan)
        self.assertFalse(has_errors(check_constraints(self.plan, schedule)))
        self.assertNotIn("!", _chart_rows(render_timeline(self.plan, schedule)))

    def test_timeline_marks_a_genuine_overlap(self):
        schedule, _ = validate_schedule({
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
                {"task_id": "t2", "resource_id": "alpha",
                 "start": "2026-08-11T08:30:00", "end": "2026-08-11T09:30:00"},
            ],
        }, self.plan)
        self.assertIn("over_capacity", codes(check_constraints(self.plan, schedule)))
        self.assertIn("!", _chart_rows(render_timeline(self.plan, schedule)))

    def test_summary_counts(self):
        metrics = summarize(self.plan, self.schedule)
        self.assertEqual(metrics["tasks_scheduled"], 2)
        self.assertEqual(metrics["tasks_unscheduled"], 0)
        self.assertEqual(metrics["makespan_minutes"], 120)

    def test_hallucinated_task_does_not_inflate_the_count(self):
        # Regression: an unknown task id once produced "4 of 3 scheduled".
        schedule, _ = validate_schedule({
            "session": "t-001",
            "assignments": [
                {"task_id": "t1", "resource_id": "alpha",
                 "start": "2026-08-11T08:00:00", "end": "2026-08-11T09:00:00"},
                {"task_id": "t2", "resource_id": "bravo",
                 "start": "2026-08-11T09:00:00", "end": "2026-08-11T10:00:00"},
                {"task_id": "ghost", "resource_id": "bravo",
                 "start": "2026-08-11T11:00:00", "end": "2026-08-11T12:00:00"},
            ],
        }, self.plan)
        metrics = summarize(self.plan, schedule)
        self.assertEqual(metrics["tasks_scheduled"], 2)
        self.assertEqual(metrics["tasks_total"], 2)
        self.assertEqual(metrics["tasks_unscheduled"], 0)

    def test_packet_contains_plan_and_contract(self):
        packet = build_packet(self.plan)
        self.assertIn("t-001", packet)
        self.assertIn("assignments", packet)
        self.assertIn("```json", packet)


class Storage(unittest.TestCase):
    def test_append_only_index_and_run_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.ensure_dirs()
            run_id = store.new_run_id("demo session")
            store.create_run(run_id)
            store.write_json(run_id, "plan.json", {"ok": True})

            store.append_event({"event": "stage", "run_id": run_id})
            store.append_event({"event": "ingest", "run_id": run_id, "valid": True})
            events = store.read_events()
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event"], "stage")

            # appending again must not disturb earlier lines
            store.append_event({"event": "ingest", "run_id": run_id, "valid": False})
            events = store.read_events()
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["event"], "stage")

            self.assertEqual(store.resolve_run(None), run_id)
            self.assertEqual(store.resolve_run(run_id[:10]), run_id)
            self.assertIsNone(store.resolve_run("nope"))


GOOD_REPLY = """The model's prose goes here.

```json
{
  "session": "s1",
  "assignments": [
    {"task_id": "t1", "resource_id": "alpha",
     "start": "2026-08-11T08:00:00", "end": "2026-08-11T10:00:00"},
    {"task_id": "t2", "resource_id": "alpha",
     "start": "2026-08-11T10:00:00", "end": "2026-08-11T11:30:00"},
    {"task_id": "t3", "resource_id": "alpha",
     "start": "2026-08-11T11:30:00", "end": "2026-08-11T12:30:00"}
  ],
  "unscheduled": [],
  "rationale": "Sequential chain."
}
```
"""


class CommandLine(unittest.TestCase):
    """End-to-end exercise of the new -> stage -> ingest cycle."""

    def _run(self, argv):
        import contextlib
        import io as _io

        from mitss.cli import main

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_full_cycle_records_model_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run(["--root", tmp, "new", "s1"])
            self.assertEqual(code, 0)

            code, out = self._run(["--root", tmp, "stage"])
            self.assertEqual(code, 0, out)

            store = RunStore(tmp)
            run_id = store.latest_run()
            self.assertIsNotNone(run_id)
            self.assertIsNotNone(store.read_text(run_id, "packet.md"))

            reply_path = os.path.join(tmp, "reply.md")
            with open(reply_path, "w", encoding="utf-8") as handle:
                handle.write(GOOD_REPLY)

            code, out = self._run(
                ["--root", tmp, "ingest", "--file", reply_path,
                 "--model", "some-other-llm", "--note", "first pass"]
            )
            self.assertEqual(code, 0, out)

            meta = store.read_json(run_id, "meta.json")
            self.assertEqual(meta["model"], "some-other-llm")
            self.assertEqual(meta["note"], "first pass")

            events = store.read_events()
            ingest_events = [e for e in events if e["event"] == "ingest"]
            self.assertEqual(ingest_events[-1]["model"], "some-other-llm")
            self.assertTrue(ingest_events[-1]["valid"])

            # artifacts written
            self.assertIsNotNone(store.read_json(run_id, "schedule.json"))
            self.assertIsNotNone(store.read_json(run_id, "summary.json"))
            self.assertIsNotNone(store.read_text(run_id, "schedule.csv"))

    def test_illegal_reply_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(["--root", tmp, "new", "s1"])
            self._run(["--root", tmp, "stage"])
            reply_path = os.path.join(tmp, "bad.md")
            with open(reply_path, "w", encoding="utf-8") as handle:
                handle.write(GOOD_REPLY.replace("2026-08-11T10:00:00", "2026-08-11T09:00:00"))
            code, out = self._run(["--root", tmp, "ingest", "--file", reply_path,
                                   "--model", "some-other-llm"])
            self.assertEqual(code, 1)
            self.assertIn("duration_mismatch", out)

    def test_missing_model_is_flagged_but_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(["--root", tmp, "new", "s1"])
            self._run(["--root", tmp, "stage"])
            reply_path = os.path.join(tmp, "reply.md")
            with open(reply_path, "w", encoding="utf-8") as handle:
                handle.write(GOOD_REPLY)
            code, out = self._run(["--root", tmp, "ingest", "--file", reply_path])
            self.assertEqual(code, 0)
            self.assertIn("no model recorded", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
