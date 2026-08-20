"""Comparison: the version-by-model matrix, and diffing two pieces of text.

Since the harness passes prompts straight through and does not judge outputs,
comparison is where the value sits. Two questions matter:

  - Across the grid, where have I already looked and what did I conclude?
  - Between these two outputs specifically, what actually differs?

Diffing uses difflib from the standard library, at word granularity, because
line diffs are close to useless on model output — a reworded sentence shows up
as one deleted line and one added line with no indication of what changed
inside it.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

from .models import ACCURATE, INACCURATE, PARTIAL, UNRATED, Run

# Split into words while keeping the whitespace, so a rebuilt string is
# identical to the original and rendering preserves the author's formatting.
_TOKEN = re.compile(r"\S+\s*")


def tokenize(text: str) -> List[str]:
    text = text or ""
    tokens = _TOKEN.findall(text)
    rebuilt = sum(map(len, tokens))
    if rebuilt != len(text):
        # \S+\s* cannot match a leading whitespace run, and that is the only
        # thing it can miss. Keep it as its own token, or an output starting
        # with a blank line would render with that line silently stripped.
        tokens.insert(0, text[: len(text) - rebuilt])
    return tokens


def diff_text(before: str, after: str) -> Dict[str, Any]:
    """Word-level diff returning render-ready spans for both sides.

    Each side is a list of {text, kind} where kind is "same", "removed" (left
    only) or "added" (right only). Concatenating the text of one side
    reproduces that side exactly.
    """
    left_tokens = tokenize(before)
    right_tokens = tokenize(after)
    matcher = difflib.SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)

    left_spans: List[Dict[str, str]] = []
    right_spans: List[Dict[str, str]] = []
    added = removed = same = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        left_chunk = "".join(left_tokens[i1:i2])
        right_chunk = "".join(right_tokens[j1:j2])

        if tag == "equal":
            same += i2 - i1
            if left_chunk:
                left_spans.append({"text": left_chunk, "kind": "same"})
            if right_chunk:
                right_spans.append({"text": right_chunk, "kind": "same"})
        elif tag == "delete":
            removed += i2 - i1
            left_spans.append({"text": left_chunk, "kind": "removed"})
        elif tag == "insert":
            added += j2 - j1
            right_spans.append({"text": right_chunk, "kind": "added"})
        else:  # replace
            removed += i2 - i1
            added += j2 - j1
            left_spans.append({"text": left_chunk, "kind": "removed"})
            right_spans.append({"text": right_chunk, "kind": "added"})

    total = same + added + removed
    return {
        "left": left_spans,
        "right": right_spans,
        "added_words": added,
        "removed_words": removed,
        "unchanged_words": same,
        "similarity": round(matcher.ratio(), 4),
        "identical": added == 0 and removed == 0 and total > 0,
    }


def compare_runs(left: Run, right: Run) -> Dict[str, Any]:
    """Full side-by-side payload for two runs."""
    return {
        "left": left.summary(),
        "right": right.summary(),
        "output_diff": diff_text(left.output, right.output),
        "prompt_diff": diff_text(left.prompt_text, right.prompt_text),
        "same_prompt_version": (
            left.prompt_id == right.prompt_id and left.version == right.version
        ),
        "same_model": left.model == right.model,
    }


def build_matrix(runs: List[Run], versions: List[int],
                 models: Optional[List[str]] = None) -> Dict[str, Any]:
    """Prompt versions down the side, models across the top.

    Each cell holds every run for that pairing, newest first, plus the cell's
    headline verdict. A cell with any inaccurate run reads as inaccurate even
    if a later attempt passed, because a model that got it wrong once is the
    thing worth noticing.

    Callers filter `runs` to a single input before calling if they want a
    like-for-like comparison; passing runs across several inputs aggregates
    them, which is why each cell also reports how many distinct inputs it
    covers. Comparing v1 on one passage against v2 on a different passage
    tells you nothing, so the interface says which case it is showing.
    """
    model_list = models if models is not None else []
    if not model_list:
        seen: List[str] = []
        for run in runs:
            label = run.model or "unnamed"
            if label not in seen:
                seen.append(label)
        model_list = sorted(seen)

    cells: Dict[str, Dict[str, Any]] = {}
    for version in versions:
        for model in model_list:
            matching = [
                r for r in runs
                if r.version == version and (r.model or "unnamed") == model
            ]
            matching.sort(key=lambda r: r.created_at or "", reverse=True)
            cells[f"{version}|{model}"] = {
                "version": version,
                "model": model,
                "count": len(matching),
                "verdict": headline_verdict(matching),
                "inputs_covered": len({r.input_id for r in matching}),
                "runs": [r.summary() for r in matching],
            }

    missing = [
        {"version": version, "model": model}
        for version in versions for model in model_list
        if cells[f"{version}|{model}"]["count"] == 0
    ]

    return {
        "versions": versions,
        "models": model_list,
        "cells": cells,
        "totals": tally(runs),
        "missing": missing,
        "inputs_in_view": sorted({r.input_id for r in runs}),
    }


def headline_verdict(runs: List[Run]) -> Optional[str]:
    """The verdict a cell shows. Worst recorded outcome wins."""
    if not runs:
        return None
    verdicts = {r.verdict for r in runs}
    for candidate in (INACCURATE, PARTIAL, ACCURATE):
        if candidate in verdicts:
            return candidate
    return UNRATED


def tally(runs: List[Run]) -> Dict[str, int]:
    counts = {UNRATED: 0, ACCURATE: 0, PARTIAL: 0, INACCURATE: 0}
    for run in runs:
        counts[run.verdict] = counts.get(run.verdict, 0) + 1
    counts["total"] = len(runs)
    return counts
