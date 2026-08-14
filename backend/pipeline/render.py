"""Combine a prompt template with an input set into the final prompt.

The split matters. A prompt version is the *wording* — the instructions you are
tuning. An input is the *material* those instructions are applied to. Keeping
them apart is what makes the version history mean something: v1 against v2 on
the same input is a fair comparison of wording, and one input run across every
version answers "did my rewrite actually help".

Rendering is deliberately dumb — one placeholder, straight substitution, no
expression language. A template engine here would be a second thing to debug
when a prompt misbehaves, and the whole point of the harness is that the prompt
you copied is the prompt that ran.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

PLACEHOLDER = "{input}"

ERROR = "error"
WARN = "warn"
INFO = "info"


def issue(severity: str, code: str, message: str) -> Dict[str, str]:
    return {"severity": severity, "code": code, "message": message, "where": "render"}


def has_placeholder(template: str) -> bool:
    return PLACEHOLDER in (template or "")


def render_prompt(template: str, input_text: str = "") -> Tuple[str, List[Dict[str, str]]]:
    """Return (final_prompt, notes).

    Three cases:
      - template contains {input}  -> substitute, wherever it sits
      - no input given             -> the template is the prompt, unchanged
      - input given, no placeholder-> append it, and say so
    """
    template = template or ""
    input_text = input_text or ""
    notes: List[Dict[str, str]] = []

    if has_placeholder(template):
        if not input_text.strip():
            notes.append(issue(
                WARN, "empty_input",
                "the prompt has an {input} placeholder but no input was chosen, "
                "so the placeholder was replaced with nothing",
            ))
        rendered = template.replace(PLACEHOLDER, input_text)
        if template.count(PLACEHOLDER) > 1:
            notes.append(issue(
                INFO, "repeated_placeholder",
                f"the input was substituted in {template.count(PLACEHOLDER)} places",
            ))
        return rendered, notes

    if input_text.strip():
        notes.append(issue(
            WARN, "no_placeholder",
            "the prompt has no {input} placeholder, so the input was appended at "
            "the end. Add {input} where you want it to sit instead.",
        ))
        return template.rstrip() + "\n\n" + input_text, notes

    return template, notes


def preview(template: str, input_text: str = "", limit: int = 4000) -> Dict[str, Any]:
    """Rendered prompt plus the numbers worth showing before sending it."""
    rendered, notes = render_prompt(template, input_text)
    truncated = len(rendered) > limit
    return {
        "rendered": rendered[:limit],
        "truncated": truncated,
        "characters": len(rendered),
        "words": len(rendered.split()),
        "has_placeholder": has_placeholder(template),
        "notes": notes,
    }
