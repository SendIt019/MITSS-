"""Compile everything recorded so far into one digestible summary.

The per-run folders are complete but atomised; the transcript is readable but
chronological. The digest is the third view: the whole library rolled up by
prompt, version and model, so "where do we stand" has a one-screen answer.

It compiles the operator's own verdicts — it never judges an output itself,
which keeps the capture-only rule intact. Two renderings share one build:
a dict for the API and the interface, and a plain-text page meant to be read
by a person or pasted into a model as context.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from .compare import tally
from .models import ModelEntry, Prompt, Run, UNRATED, VERDICT_LABELS


def build_digest(prompts: List[Prompt], runs: List[Run],
                 models: List[ModelEntry]) -> Dict[str, Any]:
    """Roll every recorded run up by prompt, version and model."""
    by_prompt: Dict[str, List[Run]] = {}
    for run in runs:
        by_prompt.setdefault(run.prompt_id, []).append(run)

    prompt_blocks = []
    for prompt in prompts:
        mine = by_prompt.get(prompt.id, [])
        prompt_blocks.append({
            "id": prompt.id,
            "name": prompt.name,
            "version_count": len(prompt.versions),
            "latest_version": prompt.latest.version if prompt.latest else None,
            "totals": tally(mine),
            "versions": [
                {
                    "version": v.version,
                    "note": v.note,
                    "totals": tally([r for r in mine if r.version == v.version]),
                }
                for v in prompt.versions
            ],
            "models": _by_model(mine),
            "unreviewed": sum(1 for r in mine if r.verdict == UNRATED),
        })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "totals": tally(runs),
        "unreviewed": sum(1 for r in runs if r.verdict == UNRATED),
        "prompts": prompt_blocks,
        "models": _by_model(runs),
        "registered_models": [m.summary() for m in models],
    }


def _by_model(runs: List[Run]) -> List[Dict[str, Any]]:
    labels: List[str] = []
    for run in runs:
        label = run.model or "unnamed"
        if label not in labels:
            labels.append(label)
    return [
        {"model": label,
         "totals": tally([r for r in runs if (r.model or "unnamed") == label])}
        for label in sorted(labels)
    ]


def _counts(totals: Dict[str, int]) -> str:
    """One tally as a compact readable clause, only the non-zero parts."""
    parts = [
        f"{totals[key]} {VERDICT_LABELS[key]}"
        for key in ("accurate", "partial", "inaccurate", "unrated")
        if totals.get(key)
    ]
    return ", ".join(parts) if parts else "no runs"


def digest_text(digest: Dict[str, Any]) -> str:
    """The digest as a plain-text page.

    Written to be read top to bottom by someone with no tool installed, and
    compact enough to paste into a model as situational context.
    """
    lines: List[str] = []
    push = lines.append

    push("MITSS DIGEST")
    push(f"generated {digest['generated_at']}")
    push("=" * 72)
    push("")

    totals = digest["totals"]
    push(f"OVERALL: {totals['total']} recorded output"
         f"{'' if totals['total'] == 1 else 's'} ({_counts(totals)})")
    if digest["unreviewed"]:
        push(f"REVIEW QUEUE: {digest['unreviewed']} output"
             f"{'' if digest['unreviewed'] == 1 else 's'} not read yet")
    push("")

    registered = digest.get("registered_models", [])
    if registered:
        push("REGISTERED MODELS")
        for entry in registered:
            reach = "backend can call it" if entry["callable"] else "paste-only"
            owner = f" (owner: {entry['owner']})" if entry["owner"] else ""
            push(f"  {entry['name']}{owner} - {reach}")
        push("")

    if digest["models"]:
        push("BY MODEL (all prompts)")
        for block in digest["models"]:
            push(f"  {block['model']}: {_counts(block['totals'])}")
        push("")

    for prompt in digest["prompts"]:
        push("-" * 72)
        push(f"PROMPT: {prompt['name']} [{prompt['id']}] - "
             f"{prompt['version_count']} version"
             f"{'' if prompt['version_count'] == 1 else 's'}, "
             f"{_counts(prompt['totals'])}")
        for version in prompt["versions"]:
            note = f" ({version['note']})" if version["note"] else ""
            push(f"  v{version['version']}{note}: {_counts(version['totals'])}")
        for block in prompt["models"]:
            push(f"  on {block['model']}: {_counts(block['totals'])}")
        if prompt["unreviewed"]:
            push(f"  still to review: {prompt['unreviewed']}")
    if digest["prompts"]:
        push("-" * 72)

    push("")
    return "\n".join(lines)
