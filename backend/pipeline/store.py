"""Filesystem storage for prompts, versions and runs.

Everything is plain files on disk — readable, greppable, and survivable without
this application. A prompt version is a `.txt` file you could open in any
editor; a run's output is a `.txt` file beside its metadata. No database, and
nothing in the storage layer that a future tool could not read.

Layout:

    data/
      prompts/
        <prompt-id>/
          prompt.json          name, tags, version index
          v1.txt, v2.txt, ...  the exact text of each revision
      runs/
        <run-id>/
          run.json             model, verdict, notes, timestamps
          prompt.txt           the prompt exactly as sent
          output.txt           the output exactly as returned
      index.jsonl              append-only event log
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import Prompt, PromptVersion, Run, UNRATED, slugify

INDEX_NAME = "index.jsonl"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class NotFound(Exception):
    """Raised when a prompt, version or run does not exist."""


class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.data_dir = os.path.join(self.root, "data")
        self.prompts_dir = os.path.join(self.data_dir, "prompts")
        self.runs_dir = os.path.join(self.data_dir, "runs")
        self.ensure()

    def ensure(self) -> None:
        os.makedirs(self.prompts_dir, exist_ok=True)
        os.makedirs(self.runs_dir, exist_ok=True)

    # -- low level -------------------------------------------------------

    @staticmethod
    def _read_text(path: str) -> Optional[str]:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _write_text(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _read_json(path: str) -> Optional[Any]:
        text = Store._read_text(path)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _write_json(path: str, payload: Any) -> None:
        Store._write_text(path, json.dumps(payload, indent=2) + "\n")

    # -- prompts ---------------------------------------------------------

    def prompt_dir(self, prompt_id: str) -> str:
        return os.path.join(self.prompts_dir, prompt_id)

    def list_prompt_ids(self) -> List[str]:
        if not os.path.isdir(self.prompts_dir):
            return []
        return sorted(
            name for name in os.listdir(self.prompts_dir)
            if os.path.isdir(os.path.join(self.prompts_dir, name))
        )

    def unique_prompt_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        counter = 2
        while os.path.exists(self.prompt_dir(candidate)):
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def create_prompt(self, name: str, text: str, note: str = "",
                      tags: Optional[List[str]] = None) -> Prompt:
        prompt_id = self.unique_prompt_id(name)
        created = now()
        prompt = Prompt(id=prompt_id, name=name or prompt_id, created_at=created,
                        tags=list(tags or []))
        self._write_json(
            os.path.join(self.prompt_dir(prompt_id), "prompt.json"),
            {"id": prompt_id, "name": prompt.name, "created_at": created,
             "tags": prompt.tags, "versions": []},
        )
        # Log the creation before the first version, so the append-only index
        # never shows a version arriving at a prompt that does not exist yet.
        self.append_event({"event": "prompt_created", "prompt_id": prompt_id,
                           "name": prompt.name})
        self.add_version(prompt_id, text, note or "first version")
        return self.get_prompt(prompt_id)

    def get_prompt(self, prompt_id: str) -> Prompt:
        meta = self._read_json(os.path.join(self.prompt_dir(prompt_id), "prompt.json"))
        if meta is None:
            raise NotFound(f"no prompt '{prompt_id}'")

        versions: List[PromptVersion] = []
        for entry in meta.get("versions", []):
            number = entry.get("version")
            text = self._read_text(
                os.path.join(self.prompt_dir(prompt_id), f"v{number}.txt")
            )
            versions.append(
                PromptVersion(
                    version=number,
                    text=text if text is not None else "",
                    note=entry.get("note", ""),
                    created_at=entry.get("created_at", ""),
                )
            )
        versions.sort(key=lambda v: v.version)

        return Prompt(
            id=meta["id"],
            name=meta.get("name", meta["id"]),
            created_at=meta.get("created_at", ""),
            tags=meta.get("tags", []),
            versions=versions,
        )

    def add_version(self, prompt_id: str, text: str, note: str = "") -> PromptVersion:
        path = os.path.join(self.prompt_dir(prompt_id), "prompt.json")
        meta = self._read_json(path)
        if meta is None:
            raise NotFound(f"no prompt '{prompt_id}'")

        entries = meta.get("versions", [])
        number = max((e.get("version", 0) for e in entries), default=0) + 1
        created = now()

        self._write_text(os.path.join(self.prompt_dir(prompt_id), f"v{number}.txt"), text)
        entries.append({"version": number, "note": note, "created_at": created})
        meta["versions"] = entries
        self._write_json(path, meta)

        self.append_event({"event": "version_added", "prompt_id": prompt_id,
                           "version": number, "note": note})
        return PromptVersion(version=number, text=text, note=note, created_at=created)

    def rename_prompt(self, prompt_id: str, name: str) -> Prompt:
        path = os.path.join(self.prompt_dir(prompt_id), "prompt.json")
        meta = self._read_json(path)
        if meta is None:
            raise NotFound(f"no prompt '{prompt_id}'")
        meta["name"] = name
        self._write_json(path, meta)
        return self.get_prompt(prompt_id)

    def list_prompts(self) -> List[Prompt]:
        out = []
        for prompt_id in self.list_prompt_ids():
            try:
                out.append(self.get_prompt(prompt_id))
            except NotFound:
                continue
        out.sort(key=lambda p: p.summary()["updated_at"] or "", reverse=True)
        return out

    # -- runs ------------------------------------------------------------

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, run_id)

    def list_run_ids(self) -> List[str]:
        if not os.path.isdir(self.runs_dir):
            return []
        return sorted(
            name for name in os.listdir(self.runs_dir)
            if os.path.isdir(os.path.join(self.runs_dir, name))
        )

    def unique_run_id(self, prompt_id: str, version: int) -> str:
        base = f"{stamp()}-{prompt_id}-v{version}"
        candidate = base
        counter = 2
        while os.path.exists(self.run_dir(candidate)):
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def create_run(self, prompt_id: str, version: int, model: str, output: str,
                   notes: str = "", verdict: str = UNRATED, source: str = "paste",
                   duration_ms: Optional[int] = None) -> Run:
        prompt = self.get_prompt(prompt_id)
        prompt_version = prompt.version(version)
        if prompt_version is None:
            raise NotFound(f"prompt '{prompt_id}' has no version {version}")

        run_id = self.unique_run_id(prompt_id, version)
        run = Run(
            id=run_id,
            prompt_id=prompt_id,
            version=version,
            model=model,
            output=output,
            prompt_text=prompt_version.text,
            verdict=verdict,
            notes=notes,
            created_at=now(),
            source=source,
            duration_ms=duration_ms,
        )
        self._persist_run(run)
        self.append_event({
            "event": "run_recorded", "run_id": run_id, "prompt_id": prompt_id,
            "version": version, "model": model, "verdict": verdict,
            "source": source, "output_characters": len(output),
        })
        return run

    def _persist_run(self, run: Run) -> None:
        directory = self.run_dir(run.id)
        os.makedirs(directory, exist_ok=True)
        self._write_text(os.path.join(directory, "prompt.txt"), run.prompt_text)
        self._write_text(os.path.join(directory, "output.txt"), run.output)
        payload = run.summary()
        self._write_json(os.path.join(directory, "run.json"), payload)

    def get_run(self, run_id: str) -> Run:
        directory = self.run_dir(run_id)
        meta = self._read_json(os.path.join(directory, "run.json"))
        if meta is None:
            raise NotFound(f"no run '{run_id}'")
        run = Run.from_dict(meta)
        run.output = self._read_text(os.path.join(directory, "output.txt")) or ""
        run.prompt_text = self._read_text(os.path.join(directory, "prompt.txt")) or ""
        return run

    def list_runs(self, prompt_id: Optional[str] = None,
                  version: Optional[int] = None,
                  model: Optional[str] = None) -> List[Run]:
        runs = []
        for run_id in self.list_run_ids():
            try:
                run = self.get_run(run_id)
            except NotFound:
                continue
            if prompt_id and run.prompt_id != prompt_id:
                continue
            if version is not None and run.version != version:
                continue
            if model and run.model != model:
                continue
            runs.append(run)
        runs.sort(key=lambda r: r.created_at or "", reverse=True)
        return runs

    def review_run(self, run_id: str, verdict: Optional[str] = None,
                   notes: Optional[str] = None) -> Run:
        run = self.get_run(run_id)
        if verdict is not None:
            run.verdict = verdict
        if notes is not None:
            run.notes = notes
        run.reviewed_at = now()
        self._persist_run(run)
        self.append_event({"event": "run_reviewed", "run_id": run_id,
                           "verdict": run.verdict})
        return run

    def delete_run(self, run_id: str) -> None:
        """Runs are removable; prompts and versions are not."""
        directory = self.run_dir(run_id)
        if not os.path.isdir(directory):
            raise NotFound(f"no run '{run_id}'")
        for name in os.listdir(directory):
            os.remove(os.path.join(directory, name))
        os.rmdir(directory)
        self.append_event({"event": "run_deleted", "run_id": run_id})

    def models_used(self, prompt_id: Optional[str] = None) -> List[str]:
        seen = []
        for run in self.list_runs(prompt_id=prompt_id):
            label = run.model or "unnamed"
            if label not in seen:
                seen.append(label)
        return sorted(seen)

    # -- append-only index ------------------------------------------------

    @property
    def index_path(self) -> str:
        return os.path.join(self.data_dir, INDEX_NAME)

    def append_event(self, event: Dict[str, Any]) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        record = dict(event)
        record.setdefault("ts", now())
        with open(self.index_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def read_events(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return []
        events = []
        with open(self.index_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events[-limit:] if limit else events
