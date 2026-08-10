"""Run storage and the append-only event log.

Every run is a folder under runs/ holding the exact plan, the packet that was
generated from it, the raw model output, the parsed schedule, and the issues
found. runs/index.jsonl is an append-only JSON Lines log: one line per event,
never rewritten, so run history survives even if a run folder is deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

INDEX_NAME = "index.jsonl"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RunStore:
    """Filesystem layout for runs, plus the append-only index."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.runs_dir = os.path.join(self.root, "runs")
        self.inputs_dir = os.path.join(self.root, "inputs")

    # -- paths ------------------------------------------------------------

    def ensure_dirs(self) -> None:
        os.makedirs(self.runs_dir, exist_ok=True)
        os.makedirs(self.inputs_dir, exist_ok=True)

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, run_id)

    def path_in_run(self, run_id: str, name: str) -> str:
        return os.path.join(self.run_dir(run_id), name)

    @property
    def index_path(self) -> str:
        return os.path.join(self.runs_dir, INDEX_NAME)

    # -- run lifecycle ----------------------------------------------------

    def new_run_id(self, session: str) -> str:
        base = f"{now_stamp()}-{_slug(session)}"
        candidate = base
        counter = 2
        while os.path.exists(self.run_dir(candidate)):
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def create_run(self, run_id: str) -> str:
        path = self.run_dir(run_id)
        os.makedirs(path, exist_ok=True)
        return path

    def list_runs(self) -> List[str]:
        if not os.path.isdir(self.runs_dir):
            return []
        entries = [
            name for name in os.listdir(self.runs_dir)
            if os.path.isdir(os.path.join(self.runs_dir, name))
        ]
        return sorted(entries)

    def latest_run(self) -> Optional[str]:
        runs = self.list_runs()
        return runs[-1] if runs else None

    def resolve_run(self, run_id: Optional[str]) -> Optional[str]:
        """Accept an exact id, a unique prefix, or None for the latest run."""
        if not run_id or run_id == "latest":
            return self.latest_run()
        runs = self.list_runs()
        if run_id in runs:
            return run_id
        matches = [r for r in runs if r.startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        return None

    # -- file helpers -----------------------------------------------------

    def write_text(self, run_id: str, name: str, text: str) -> str:
        self.create_run(run_id)
        path = self.path_in_run(run_id, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_json(self, run_id: str, name: str, payload: Any) -> str:
        return self.write_text(run_id, name, json.dumps(payload, indent=2) + "\n")

    def read_text(self, run_id: str, name: str) -> Optional[str]:
        path = self.path_in_run(run_id, name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def read_json(self, run_id: str, name: str) -> Optional[Any]:
        text = self.read_text(run_id, name)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # -- append-only index ------------------------------------------------

    def append_event(self, event: Dict[str, Any]) -> None:
        """Append one line to runs/index.jsonl. Never rewrites existing lines."""
        os.makedirs(self.runs_dir, exist_ok=True)
        record = dict(event)
        record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        with open(self.index_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def read_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return []
        events: List[Dict[str, Any]] = []
        with open(self.index_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            return events[-limit:]
        return events


def _slug(text: str) -> str:
    cleaned = [c.lower() if c.isalnum() else "-" for c in text.strip()]
    slug = "".join(cleaned).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:40] or "session"
