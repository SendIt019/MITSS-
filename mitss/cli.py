"""Command line interface for MITSS.

Typical cycle:

    python -m mitss new my-session       # scaffold an input file
    python -m mitss stage                # validate it, build the prompt packet
    ... paste packet.md to the model, paste its reply into output.raw.md ...
    python -m mitss ingest               # parse, validate, constraint-check
    python -m mitss report               # table, timeline, CSV
    python -m mitss diff RUN_A RUN_B     # compare two runs of the same plan

Exit codes: 0 clean, 1 problems found, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .capture import extract_json
from .constraints import check_constraints, summarize
from .diffing import render_diff
from .issues import Issue, count_by_severity, format_issues, has_errors
from .model import Plan, Schedule
from .packet import build_packet
from .render import render_csv, render_summary, render_table, render_timeline
from .runlog import RunStore, sha256_of
from .validate import validate_plan, validate_schedule

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(PACKAGE_DIR)

PLACEHOLDER = """<!-- Paste the model's full reply below this line, then run:
       python -m mitss ingest {run_id}
     The fenced ```json block is what gets parsed; surrounding prose is ignored. -->
"""

TEMPLATE = {
    "session": "example-001",
    "domain": "generic",
    "horizon": {"start": "2026-08-11T08:00:00", "end": "2026-08-11T18:00:00"},
    "granularity_minutes": 15,
    "objectives": [
        "schedule every task inside the horizon",
        "finish as early as possible",
    ],
    "notes": "Replace this with any context the model should know.",
    "resources": [
        {"id": "alpha", "name": "Team Alpha", "capacity": 1, "available": []},
        {"id": "bravo", "name": "Team Bravo", "capacity": 1, "available": []},
    ],
    "tasks": [
        {
            "id": "t1",
            "name": "Site survey",
            "duration_minutes": 120,
            "depends_on": [],
            "requires": [],
            "priority": 1,
        },
        {
            "id": "t2",
            "name": "Equipment setup",
            "duration_minutes": 90,
            "depends_on": ["t1"],
            "requires": [],
            "priority": 2,
        },
        {
            "id": "t3",
            "name": "Calibration run",
            "duration_minutes": 60,
            "depends_on": ["t2"],
            "requires": ["alpha"],
            "priority": 2,
        },
    ],
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _print_issues(issues: List[Issue], header: str) -> None:
    if not issues:
        return
    counts = count_by_severity(issues)
    print(f"\n{header} ({counts['error']} error, {counts['warn']} warn, {counts['info']} info)")
    print(format_issues(issues))


def _load_plan_file(path: str):
    if not os.path.exists(path):
        return None, [f"input file not found: {path}"]
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"{path} is not valid JSON: {exc}"]
    return data, []


def _pick_input(store: RunStore, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    if not os.path.isdir(store.inputs_dir):
        return None
    candidates = sorted(
        name for name in os.listdir(store.inputs_dir) if name.endswith(".json")
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"note: {len(candidates)} input files found, using the first: {candidates[0]}")
        print("      pass --input to choose a different one")
    return os.path.join(store.inputs_dir, candidates[0])


def _rebuild(store: RunStore, run_id: str):
    """Load the stored plan and schedule for a completed run."""
    plan_data = store.read_json(run_id, "plan.json")
    if plan_data is None:
        return None, None, "no plan.json in this run"
    plan, plan_issues = validate_plan(plan_data)
    if plan is None:
        return None, None, "stored plan.json no longer validates: " + format_issues(plan_issues)
    schedule_data = store.read_json(run_id, "schedule.json")
    if schedule_data is None:
        return plan, None, "no schedule.json yet - run ingest first"
    schedule, _ = validate_schedule(schedule_data, plan)
    if schedule is None:
        return plan, None, "stored schedule.json no longer validates"
    return plan, schedule, None


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_new(args, store: RunStore) -> int:
    store.ensure_dirs()
    name = args.name or "example-001"
    filename = name if name.endswith(".json") else f"{name}.json"
    path = os.path.join(store.inputs_dir, filename)
    if os.path.exists(path) and not args.force:
        print(f"refusing to overwrite {path} (pass --force to replace it)")
        return 2
    payload = dict(TEMPLATE)
    payload["session"] = os.path.splitext(filename)[0]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {path}")
    print("edit it, then run: python -m mitss stage")
    return 0


def cmd_check(args, store: RunStore) -> int:
    path = _pick_input(store, args.input)
    if path is None:
        print("no input file found in inputs/ - run: python -m mitss new")
        return 2
    data, errors = _load_plan_file(path)
    if errors:
        for message in errors:
            print(message)
        return 1
    plan, issues = validate_plan(data)
    _print_issues(issues, f"plan check: {os.path.basename(path)}")
    if plan is None:
        print("\nplan is not usable - fix the errors above")
        return 1
    print(f"\nplan OK: {len(plan.tasks)} tasks, {len(plan.resources)} resources, "
          f"horizon {plan.horizon.minutes} min")
    return 0


def cmd_stage(args, store: RunStore) -> int:
    store.ensure_dirs()
    path = _pick_input(store, args.input)
    if path is None:
        print("no input file found in inputs/ - run: python -m mitss new")
        return 2

    data, errors = _load_plan_file(path)
    if errors:
        for message in errors:
            print(message)
        return 1

    plan, issues = validate_plan(data)
    _print_issues(issues, f"plan check: {os.path.basename(path)}")
    if plan is None:
        print("\nnothing staged - fix the plan errors above")
        return 1

    run_id = store.new_run_id(plan.session)
    store.create_run(run_id)
    store.write_json(run_id, "plan.json", plan.to_dict())
    packet = build_packet(plan)
    packet_path = store.write_text(run_id, "packet.md", packet)
    raw_path = store.write_text(run_id, "output.raw.md", PLACEHOLDER.format(run_id=run_id))

    store.append_event(
        {
            "event": "stage",
            "run_id": run_id,
            "session": plan.session,
            "source_input": os.path.relpath(path, store.root),
            "tasks": len(plan.tasks),
            "resources": len(plan.resources),
            "plan_sha": sha256_of(json.dumps(plan.to_dict(), sort_keys=True)),
            "plan_warnings": count_by_severity(issues)["warn"],
        }
    )

    print(f"\nstaged run {run_id}")
    print(f"  packet : {os.path.relpath(packet_path, store.root)}")
    print(f"  paste  : {os.path.relpath(raw_path, store.root)}")
    print("\nnext: send packet.md to the model, paste the reply into output.raw.md, then run")
    print(f"      python -m mitss ingest {run_id}")
    return 0


def cmd_ingest(args, store: RunStore) -> int:
    run_id = store.resolve_run(args.run_id)
    if run_id is None:
        print("no matching run - run: python -m mitss stage")
        return 2

    if args.stdin:
        raw = sys.stdin.read()
        store.write_text(run_id, "output.raw.md", raw)
    elif args.file:
        if not os.path.exists(args.file):
            print(f"file not found: {args.file}")
            return 2
        with open(args.file, "r", encoding="utf-8") as handle:
            raw = handle.read()
        store.write_text(run_id, "output.raw.md", raw)
    else:
        raw = store.read_text(run_id, "output.raw.md") or ""

    if not raw.strip() or raw.strip() == PLACEHOLDER.format(run_id=run_id).strip():
        print(f"output.raw.md for {run_id} is still empty - paste the model reply into")
        print(f"  {os.path.relpath(store.path_in_run(run_id, 'output.raw.md'), store.root)}")
        return 2

    plan_data = store.read_json(run_id, "plan.json")
    plan, _ = validate_plan(plan_data) if plan_data else (None, [])
    if plan is None:
        print(f"run {run_id} has no usable plan.json")
        return 1

    all_issues: List[Issue] = []
    parsed, capture_issues = extract_json(raw)
    all_issues.extend(capture_issues)

    schedule: Optional[Schedule] = None
    if parsed is not None:
        schedule, schema_issues = validate_schedule(parsed, plan)
        all_issues.extend(schema_issues)
        if schedule is not None:
            all_issues.extend(check_constraints(plan, schedule))

    _print_issues(all_issues, f"ingest {run_id}")

    counts = count_by_severity(all_issues)
    store.write_json(run_id, "issues.json", [i.to_dict() for i in all_issues])

    if schedule is not None:
        store.write_json(run_id, "schedule.json", schedule.to_dict())
        metrics = summarize(plan, schedule)
        store.write_json(run_id, "summary.json", metrics)
        store.write_text(run_id, "schedule.csv", render_csv(plan, schedule))
        print()
        print(render_summary(metrics))

    store.append_event(
        {
            "event": "ingest",
            "run_id": run_id,
            "session": plan.session,
            "parsed": parsed is not None,
            "valid": schedule is not None and not has_errors(all_issues),
            "errors": counts["error"],
            "warnings": counts["warn"],
            "assignments": len(schedule.assignments) if schedule else 0,
            "output_sha": sha256_of(raw),
        }
    )

    if schedule is None:
        print("\nno usable schedule captured")
        return 1
    if counts["error"]:
        print(f"\n{counts['error']} constraint or schema error(s) - the schedule is not legal")
        print(f"see runs/{run_id}/issues.json")
        return 1

    print(f"\nclean run - schedule stored at runs/{run_id}/schedule.json")
    print(f"next: python -m mitss report {run_id}")
    return 0


def cmd_report(args, store: RunStore) -> int:
    run_id = store.resolve_run(args.run_id)
    if run_id is None:
        print("no matching run")
        return 2
    plan, schedule, problem = _rebuild(store, run_id)
    if plan is None or schedule is None:
        print(problem or "nothing to report")
        return 1

    wanted = args.format
    print(f"run {run_id} - session {plan.session}\n")

    if wanted in ("table", "all"):
        print(render_table(plan, schedule))
        print()
    if wanted in ("timeline", "all"):
        print(render_timeline(plan, schedule))
        print()
    if wanted in ("summary", "all"):
        summary = store.read_json(run_id, "summary.json") or summarize(plan, schedule)
        print(render_summary(summary))
        print()
    if wanted == "csv":
        print(render_csv(plan, schedule), end="")

    issues = store.read_json(run_id, "issues.json") or []
    errors = [i for i in issues if i.get("severity") == "error"]
    if errors and wanted != "csv":
        print(f"note: this run has {len(errors)} unresolved error(s); see issues.json")
    return 0


def cmd_diff(args, store: RunStore) -> int:
    run_a = store.resolve_run(args.run_a)
    run_b = store.resolve_run(args.run_b)
    if run_a is None or run_b is None:
        print("could not resolve both runs")
        return 2
    _, schedule_a, problem_a = _rebuild(store, run_a)
    _, schedule_b, problem_b = _rebuild(store, run_b)
    if schedule_a is None or schedule_b is None:
        print(problem_a or problem_b or "one of the runs has no schedule")
        return 1
    print(render_diff(schedule_a, schedule_b, run_a, run_b))
    return 0


def cmd_log(args, store: RunStore) -> int:
    events = store.read_events(limit=args.number)
    if not events:
        print("no runs logged yet")
        return 0
    for event in events:
        kind = event.get("event", "?")
        line = f"{event.get('ts', '')}  {kind:<7} {event.get('run_id', '')}"
        if kind == "stage":
            line += f"  tasks={event.get('tasks')} resources={event.get('resources')}"
        elif kind == "ingest":
            verdict = "clean" if event.get("valid") else "FAILED"
            line += (
                f"  {verdict} assignments={event.get('assignments')} "
                f"errors={event.get('errors')} warnings={event.get('warnings')}"
            )
        print(line)
    return 0


def cmd_status(args, store: RunStore) -> int:
    runs = store.list_runs()
    print(f"root    : {store.root}")
    print(f"inputs  : {len([f for f in os.listdir(store.inputs_dir)]) if os.path.isdir(store.inputs_dir) else 0} file(s)")
    print(f"runs    : {len(runs)}")
    if runs:
        latest = runs[-1]
        print(f"latest  : {latest}")
        has_schedule = store.read_json(latest, "schedule.json") is not None
        print(f"          {'ingested' if has_schedule else 'staged, awaiting output'}")
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mitss",
        description="Input/output harness for language-model scheduling runs.",
    )
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="project root holding inputs/ and runs/")
    sub = parser.add_subparsers(dest="command")

    p_new = sub.add_parser("new", help="write a starter input file into inputs/")
    p_new.add_argument("name", nargs="?", help="session name (default example-001)")
    p_new.add_argument("--force", action="store_true", help="overwrite if it exists")
    p_new.set_defaults(func=cmd_new)

    p_check = sub.add_parser("check", help="validate an input plan without staging a run")
    p_check.add_argument("--input", help="path to a plan JSON file")
    p_check.set_defaults(func=cmd_check)

    p_stage = sub.add_parser("stage", help="validate the plan and build the prompt packet")
    p_stage.add_argument("--input", help="path to a plan JSON file")
    p_stage.set_defaults(func=cmd_stage)

    p_ingest = sub.add_parser("ingest", help="parse and check the model's reply")
    p_ingest.add_argument("run_id", nargs="?", help="run id or prefix (default: latest)")
    p_ingest.add_argument("--file", help="read the reply from this file instead")
    p_ingest.add_argument("--stdin", action="store_true", help="read the reply from stdin")
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="render a stored schedule")
    p_report.add_argument("run_id", nargs="?", help="run id or prefix (default: latest)")
    p_report.add_argument("--format", default="all",
                          choices=["all", "table", "timeline", "summary", "csv"])
    p_report.set_defaults(func=cmd_report)

    p_diff = sub.add_parser("diff", help="compare two runs")
    p_diff.add_argument("run_a")
    p_diff.add_argument("run_b")
    p_diff.set_defaults(func=cmd_diff)

    p_log = sub.add_parser("log", help="show the append-only run log")
    p_log.add_argument("-n", "--number", type=int, default=20, help="how many events")
    p_log.set_defaults(func=cmd_log)

    p_status = sub.add_parser("status", help="show where things stand")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    store = RunStore(args.root)
    store.ensure_dirs()
    return args.func(args, store)


if __name__ == "__main__":
    raise SystemExit(main())
