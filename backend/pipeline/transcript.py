"""A single rolling plain-text transcript of every run.

The per-run folders and `index.jsonl` are already complete records, but neither
is something you would sit and read. This is the file you open, scroll, grep,
print, or hand to someone who does not have the tool installed.

Append-only, like the index. A verdict set after the fact is appended as its
own line rather than rewritten into the original block, so the file is always a
true history rather than a current-state summary. That means a run's block can
say "not reviewed yet" while a later line records the verdict — read the file
as a log, not a table.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

TRANSCRIPT_NAME = "transcript.txt"

HEAVY = "=" * 72
LIGHT = "-" * 72


def transcript_path(data_dir: str) -> str:
    return os.path.join(data_dir, TRANSCRIPT_NAME)


def _stamp(iso: str = "") -> str:
    """Human-readable timestamp; falls back to now if the run has none."""
    if iso:
        return iso.replace("T", " ")[:19]
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_run(run) -> str:
    """The block written when an output is recorded."""
    duration = f"  |  {run.duration_ms}ms" if run.duration_ms else ""
    lines = [
        HEAVY,
        f"{_stamp(run.created_at)}  |  {run.prompt_id} v{run.version}  |  "
        f"{run.model or 'unnamed model'}",
        f"run: {run.id}",
        f"input: {run.input_name or 'none'}  |  source: {run.source}{duration}",
        LIGHT,
        "PROMPT:",
        run.prompt_text.rstrip() or "(empty)",
        LIGHT,
        "OUTPUT:",
        run.output.rstrip() or "(empty)",
        LIGHT,
        f"VERDICT: {run.verdict}"
        + (f"  -- {run.notes}" if run.notes else
           ("  (not reviewed yet)" if run.verdict == "unrated" else "")),
        HEAVY,
        "",
    ]
    return "\n".join(lines)


def format_verdict(run) -> str:
    """The one-line entry appended when a verdict is set or changed later."""
    note = f"  -- {run.notes}" if run.notes else ""
    return (
        f"---- verdict set {_stamp(run.reviewed_at)}  |  run {run.id}  |  "
        f"{run.verdict}{note}\n\n"
    )


def append(data_dir: str, text: str) -> str:
    """Append to the transcript, creating it with a header if new."""
    os.makedirs(data_dir, exist_ok=True)
    path = transcript_path(data_dir)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        if new:
            handle.write(
                "MITSS transcript\n"
                "Every recorded output, in the order it happened. Append-only:\n"
                "a verdict set after a run appears as its own line further down,\n"
                "not edited into the block above it.\n\n"
            )
        handle.write(text)
    return path


def append_run(data_dir: str, run) -> str:
    return append(data_dir, format_run(run))


def append_verdict(data_dir: str, run) -> str:
    return append(data_dir, format_verdict(run))


def read(data_dir: str, limit: Optional[int] = None) -> str:
    """Whole transcript, or the last `limit` characters of it."""
    path = transcript_path(data_dir)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if limit is not None and len(text) > limit:
        return "... (earlier entries trimmed) ...\n\n" + text[-limit:]
    return text
