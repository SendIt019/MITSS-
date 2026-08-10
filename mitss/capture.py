"""Pull a JSON object out of whatever the model actually returned.

Models wrap answers in prose, fenced code blocks, or both. This module extracts
the payload without being clever about it: prefer a fenced ```json block, fall
back to the first balanced brace span, and report clearly when neither works.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple

from .issues import Issue, error, warn

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str) -> Tuple[Optional[Any], List[Issue]]:
    """Return (parsed_object, issues). parsed_object is None if nothing parsed."""
    issues: List[Issue] = []
    if not text or not text.strip():
        issues.append(error("empty_output", "the captured output is empty", "capture"))
        return None, issues

    candidates: List[Tuple[str, str]] = []

    for match in _FENCE.finditer(text):
        candidates.append(("fenced block", match.group(1)))

    span = _first_balanced_object(text)
    if span is not None:
        candidates.append(("brace span", span))

    stripped = text.strip()
    candidates.append(("whole file", stripped))

    for source, blob in candidates:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if source != "fenced block":
            issues.append(
                warn(
                    "loose_output",
                    f"no fenced json block found; parsed the {source} instead",
                    "capture",
                )
            )
        if len(_FENCE.findall(text)) > 1:
            issues.append(
                warn(
                    "multiple_blocks",
                    "the output contained more than one fenced block; used the first "
                    "one that parsed",
                    "capture",
                )
            )
        return parsed, issues

    issues.append(
        error(
            "unparseable_output",
            "could not find a valid JSON object in the captured output",
            "capture",
        )
    )
    return None, issues


def _first_balanced_object(text: str) -> Optional[str]:
    """Find the first {...} span, ignoring braces inside strings."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        start = text.find("{", start + 1)
    return None
