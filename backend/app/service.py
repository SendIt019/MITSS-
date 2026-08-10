"""Business logic sitting between the HTTP layer and the MITSS core.

Nothing here knows about FastAPI, so the same functions back the command line
and any future interface. The core package stays dependency-free; only this
layer and main.py touch anything installed from the package index.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from mitss.capture import extract_json
from mitss.constraints import check_constraints, summarize
from mitss.diffing import diff_schedules, diff_unscheduled
from mitss.issues import Issue, count_by_severity, has_errors
from mitss.llm import LLMError, ProviderUnavailable, get_provider
from mitss.packet import build_packet, build_structuring_packet
from mitss.render import render_csv
from mitss.runlog import RunStore, sha256_of
from mitss.textplan import looks_structured, parse_text_plan
from mitss.validate import validate_plan, validate_schedule

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Run status values surfaced to the interface.
NEEDS_LLM = "needs_llm"     # text could not be parsed; a model must structure it
READY = "ready"             # plan accepted, packet built, awaiting a schedule
INGESTED = "ingested"       # a schedule came back and passed every check
REJECTED = "rejected"       # a schedule came back and failed at least one check


class ServiceError(Exception):
    """Anything the interface should report as a clean 4xx."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def store(root: Optional[str] = None) -> RunStore:
    run_store = RunStore(root or os.environ.get("MITSS_ROOT", BACKEND_ROOT))
    run_store.ensure_dirs()
    return run_store


def issue_dicts(issues: List[Issue]) -> List[Dict[str, Any]]:
    return [i.to_dict() for i in issues]


# --------------------------------------------------------------------------
# upload and parse
# --------------------------------------------------------------------------

def upload_text(filename: str, text: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Take an uploaded .txt, try the deterministic parser, fall back to a model.

    Always creates a run so the raw upload is preserved even when parsing
    fails — a failed parse is data about the input, not a dead end.
    """
    if not text.strip():
        raise ServiceError("the uploaded file is empty")

    run_store = store(root)
    structured = looks_structured(text)
    plan_dict, parse_issues = parse_text_plan(text) if structured else (None, [])

    plan = None
    validation_issues: List[Issue] = []
    if plan_dict is not None:
        plan, validation_issues = validate_plan(plan_dict)

    all_issues = parse_issues + validation_issues
    session = plan.session if plan else _guess_session(filename)
    run_id = run_store.new_run_id(session)
    run_store.create_run(run_id)
    run_store.write_text(run_id, "source.txt", text)

    meta: Dict[str, Any] = {
        "filename": filename,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "parser": "text-grammar" if structured else "none",
    }

    if plan is not None:
        run_store.write_json(run_id, "plan.json", plan.to_dict())
        run_store.write_text(run_id, "packet.md", build_packet(plan))
        meta["status"] = READY
        run_store.write_json(run_id, "meta.json", meta)
        run_store.append_event({
            "event": "upload", "run_id": run_id, "session": plan.session,
            "status": READY, "parser": meta["parser"],
            "tasks": len(plan.tasks), "resources": len(plan.resources),
            "source_sha": sha256_of(text),
        })
        return {
            "run_id": run_id,
            "status": READY,
            "session": plan.session,
            "plan": plan.to_dict(),
            "issues": issue_dicts(all_issues),
            "packet": run_store.read_text(run_id, "packet.md"),
            "structuring_packet": None,
        }

    # Deterministic parsing did not produce a usable plan. Hand the raw text to
    # a model to structure, carrying the parser's complaints along as context.
    structuring_packet = build_structuring_packet(text, [str(i) for i in all_issues])
    run_store.write_text(run_id, "structuring_packet.md", structuring_packet)
    meta["status"] = NEEDS_LLM
    run_store.write_json(run_id, "meta.json", meta)
    run_store.append_event({
        "event": "upload", "run_id": run_id, "session": session,
        "status": NEEDS_LLM, "parser": meta["parser"],
        "parse_errors": count_by_severity(all_issues)["error"],
        "source_sha": sha256_of(text),
    })
    return {
        "run_id": run_id,
        "status": NEEDS_LLM,
        "session": session,
        "plan": None,
        "issues": issue_dicts(all_issues),
        "packet": None,
        "structuring_packet": structuring_packet,
    }


def attach_plan(run_id: str, raw: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Accept a model's structured plan for a run that needed one."""
    run_store = store(root)
    resolved = _resolve(run_store, run_id)

    parsed, capture_issues = extract_json(raw)
    if parsed is None:
        return {
            "run_id": resolved,
            "status": NEEDS_LLM,
            "plan": None,
            "issues": issue_dicts(capture_issues),
        }

    plan, plan_issues = validate_plan(parsed)
    issues = capture_issues + plan_issues
    if plan is None:
        return {
            "run_id": resolved,
            "status": NEEDS_LLM,
            "plan": None,
            "issues": issue_dicts(issues),
        }

    run_store.write_json(resolved, "plan.json", plan.to_dict())
    run_store.write_text(resolved, "packet.md", build_packet(plan))
    run_store.write_text(resolved, "plan_source.raw.md", raw)
    meta = run_store.read_json(resolved, "meta.json") or {}
    meta["status"] = READY
    meta["parser"] = "llm"
    run_store.write_json(resolved, "meta.json", meta)
    run_store.append_event({
        "event": "structure", "run_id": resolved, "session": plan.session,
        "status": READY, "tasks": len(plan.tasks), "resources": len(plan.resources),
    })

    return {
        "run_id": resolved,
        "status": READY,
        "session": plan.session,
        "plan": plan.to_dict(),
        "issues": issue_dicts(issues),
        "packet": run_store.read_text(resolved, "packet.md"),
    }


# --------------------------------------------------------------------------
# scheduling round trip
# --------------------------------------------------------------------------

def ingest_schedule(run_id: str, raw: str, model: str = "", note: str = "",
                    root: Optional[str] = None) -> Dict[str, Any]:
    """Validate and constraint-check a schedule returned by a model."""
    run_store = store(root)
    resolved = _resolve(run_store, run_id)

    plan_data = run_store.read_json(resolved, "plan.json")
    if plan_data is None:
        raise ServiceError(f"run {resolved} has no plan yet", 409)
    plan, _ = validate_plan(plan_data)
    if plan is None:
        raise ServiceError(f"run {resolved} has an unusable stored plan", 500)

    run_store.write_text(resolved, "output.raw.md", raw)

    issues: List[Issue] = []
    parsed, capture_issues = extract_json(raw)
    issues.extend(capture_issues)

    schedule = None
    if parsed is not None:
        schedule, schema_issues = validate_schedule(parsed, plan)
        issues.extend(schema_issues)
        if schedule is not None:
            issues.extend(check_constraints(plan, schedule))

    run_store.write_json(resolved, "issues.json", issue_dicts(issues))
    counts = count_by_severity(issues)
    legal = schedule is not None and not has_errors(issues)

    summary = None
    if schedule is not None:
        summary = summarize(plan, schedule)
        run_store.write_json(resolved, "schedule.json", schedule.to_dict())
        run_store.write_json(resolved, "summary.json", summary)
        run_store.write_text(resolved, "schedule.csv", render_csv(plan, schedule))

    meta = run_store.read_json(resolved, "meta.json") or {}
    if model:
        meta["model"] = model
    if note:
        meta["note"] = note
    meta["status"] = INGESTED if legal else REJECTED
    meta["ingested_at"] = datetime.now().isoformat(timespec="seconds")
    run_store.write_json(resolved, "meta.json", meta)

    run_store.append_event({
        "event": "ingest", "run_id": resolved, "session": plan.session,
        "model": meta.get("model", ""), "valid": legal,
        "errors": counts["error"], "warnings": counts["warn"],
        "assignments": len(schedule.assignments) if schedule else 0,
        "output_sha": sha256_of(raw),
    })

    return {
        "run_id": resolved,
        "status": meta["status"],
        "legal": legal,
        "model": meta.get("model", ""),
        "issues": issue_dicts(issues),
        "schedule": schedule.to_dict() if schedule else None,
        "summary": summary,
        "plan": plan.to_dict(),
    }


def solve_with_model(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Try to get a schedule from the configured provider, then ingest it.

    With the default manual provider this reports that the packet must be
    carried by hand, which is the expected path until a provider is configured.
    """
    run_store = store(root)
    resolved = _resolve(run_store, run_id)
    packet = run_store.read_text(resolved, "packet.md")
    if packet is None:
        raise ServiceError(f"run {resolved} has no packet yet", 409)

    provider = get_provider()
    try:
        raw = provider.complete(packet)
    except ProviderUnavailable as exc:
        raise ServiceError(str(exc), 409) from None
    except LLMError as exc:
        raise ServiceError(str(exc), 502) from None

    return ingest_schedule(resolved, raw, model=provider.describe().get("model")
                           or provider.name, root=root)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def list_runs(root: Optional[str] = None) -> List[Dict[str, Any]]:
    run_store = store(root)
    out = []
    for run_id in reversed(run_store.list_runs()):
        meta = run_store.read_json(run_id, "meta.json") or {}
        plan_data = run_store.read_json(run_id, "plan.json") or {}
        summary = run_store.read_json(run_id, "summary.json") or {}
        out.append({
            "run_id": run_id,
            "session": plan_data.get("session", meta.get("filename", "")),
            "status": meta.get("status", "unknown"),
            "model": meta.get("model", ""),
            "note": meta.get("note", ""),
            "uploaded_at": meta.get("uploaded_at"),
            "tasks": len(plan_data.get("tasks", [])),
            "assignments": len(
                (run_store.read_json(run_id, "schedule.json") or {}).get("assignments", [])
            ),
            "makespan_minutes": summary.get("makespan_minutes"),
        })
    return out


def run_detail(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    run_store = store(root)
    resolved = _resolve(run_store, run_id)
    meta = run_store.read_json(resolved, "meta.json") or {}
    return {
        "run_id": resolved,
        "status": meta.get("status", "unknown"),
        "model": meta.get("model", ""),
        "note": meta.get("note", ""),
        "filename": meta.get("filename", ""),
        "parser": meta.get("parser", ""),
        "plan": run_store.read_json(resolved, "plan.json"),
        "schedule": run_store.read_json(resolved, "schedule.json"),
        "summary": run_store.read_json(resolved, "summary.json"),
        "issues": run_store.read_json(resolved, "issues.json") or [],
        "packet": run_store.read_text(resolved, "packet.md"),
        "structuring_packet": run_store.read_text(resolved, "structuring_packet.md"),
        "source_text": run_store.read_text(resolved, "source.txt"),
        "output_raw": run_store.read_text(resolved, "output.raw.md"),
    }


def run_csv(run_id: str, root: Optional[str] = None) -> str:
    run_store = store(root)
    resolved = _resolve(run_store, run_id)
    csv_text = run_store.read_text(resolved, "schedule.csv")
    if csv_text is None:
        raise ServiceError(f"run {resolved} has no schedule to export", 409)
    return csv_text


def diff_runs(run_a: str, run_b: str, root: Optional[str] = None) -> Dict[str, Any]:
    run_store = store(root)
    first = _resolve(run_store, run_a)
    second = _resolve(run_store, run_b)

    schedule_a, plan_a = _load_schedule(run_store, first)
    schedule_b, _ = _load_schedule(run_store, second)

    changes = diff_schedules(schedule_a, schedule_b) + diff_unscheduled(schedule_a, schedule_b)
    return {
        "a": {
            "run_id": first,
            "model": (run_store.read_json(first, "meta.json") or {}).get("model", ""),
            "assignments": len(schedule_a.assignments),
            "makespan_minutes": schedule_a.makespan_minutes,
        },
        "b": {
            "run_id": second,
            "model": (run_store.read_json(second, "meta.json") or {}).get("model", ""),
            "assignments": len(schedule_b.assignments),
            "makespan_minutes": schedule_b.makespan_minutes,
        },
        "identical": not changes,
        "changes": changes,
    }


def llm_status() -> Dict[str, Any]:
    return get_provider().describe()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _resolve(run_store: RunStore, run_id: str) -> str:
    resolved = run_store.resolve_run(run_id)
    if resolved is None:
        raise ServiceError(f"no run matching '{run_id}'", 404)
    return resolved


def _load_schedule(run_store: RunStore, run_id: str):
    plan_data = run_store.read_json(run_id, "plan.json")
    schedule_data = run_store.read_json(run_id, "schedule.json")
    if plan_data is None or schedule_data is None:
        raise ServiceError(f"run {run_id} has no schedule to compare", 409)
    plan, _ = validate_plan(plan_data)
    schedule, _ = validate_schedule(schedule_data, plan)
    if plan is None or schedule is None:
        raise ServiceError(f"run {run_id} has unusable stored data", 500)
    return schedule, plan


def _guess_session(filename: str) -> str:
    base = os.path.basename(filename or "upload")
    return os.path.splitext(base)[0] or "upload"
