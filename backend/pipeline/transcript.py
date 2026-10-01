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

import json
import os
from datetime import datetime

TRANSCRIPT_NAME = "transcript.txt"

FLOPS_UNITS = ((1e18, "EFLOPs"), (1e15, "PFLOPs"), (1e12, "TFLOPs"),
               (1e9, "GFLOPs"), (1e6, "MFLOPs"))

HEAVY = "=" * 72
LIGHT = "-" * 72


def transcript_path(data_dir: str) -> str:
    return os.path.join(data_dir, TRANSCRIPT_NAME)


def _stamp(iso: str = "") -> str:
    """Human-readable timestamp; falls back to now if the run has none."""
    if iso:
        return iso.replace("T", " ")[:19]
    # Local time on purpose, the same clock as store.now().
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005


def format_settings(settings) -> str:
    """`temperature=0  max_tokens=8000  enable_thinking=false  timeout=120s`.

    Flat so the line can be grepped; chat_template_kwargs are unpacked
    because `enable_thinking` is the one people actually look for.
    """
    parts = []
    for name, value in (settings or {}).items():
        if name == "chat_template_kwargs" and isinstance(value, dict):
            parts.extend(f"{k}={json.dumps(v)}" for k, v in value.items())
        elif name == "timeout":
            parts.append(f"timeout={value:g}s")
        else:
            parts.append(f"{name}={value}")
    return "  ".join(parts)


def format_flops(value) -> str:
    """`~1.3 PFLOPs` - the tilde marks it as an estimate."""
    if value is None:
        return ""
    for size, unit in FLOPS_UNITS:
        if value >= size:
            return f"~{value / size:.1f} {unit}"
    return f"~{value:.0f} FLOPs"


def format_usage(usage) -> str:
    """`3664 in / 8000 out tokens  |  83.0 tok/s  |  first token 4.2 s  |
    decode 7.6 tok/s  |  ~1.3 PFLOPs  |  stopped: length`.

    Runs recorded before the timing and FLOPs keys existed simply lack those
    parts.
    """
    if not usage:
        return ""
    parts = []
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if prompt_tokens is not None or completion_tokens is not None:
        parts.append(f"{prompt_tokens if prompt_tokens is not None else '?'} in / "
                     f"{completion_tokens if completion_tokens is not None else '?'}"
                     " out tokens")
    if usage.get("tokens_per_second") is not None:
        parts.append(f"{usage['tokens_per_second']} tok/s")
    if usage.get("time_to_first_token_ms") is not None:
        parts.append(f"first token {usage['time_to_first_token_ms'] / 1000:.1f} s")
    if usage.get("decode_tokens_per_second") is not None:
        parts.append(f"decode {usage['decode_tokens_per_second']} tok/s")
    if usage.get("flops_estimate") is not None:
        parts.append(format_flops(usage["flops_estimate"]))
    if usage.get("finish_reason"):
        parts.append(f"stopped: {usage['finish_reason']}")
    if usage.get("model_reported"):
        parts.append(f"server said: {usage['model_reported']}")
    return "  |  ".join(parts)


def format_run(run) -> str:
    """The block written when an output is recorded."""
    duration = f"  |  {run.duration_ms}ms" if run.duration_ms else ""
    settings = getattr(run, "settings", None)
    lines = [
        HEAVY,
        (f"{_stamp(run.created_at)}  |  {run.prompt_id} v{run.version}  |  "
        f"{run.model or 'unnamed model'}"),
        f"run: {run.id}",
        f"input: {run.input_name or 'none'}  |  source: {run.source}{duration}",
    ]
    # Only provider runs carry settings; a pasted run has nothing to say here,
    # and older blocks in the same file simply lack the line.
    if settings:
        lines.append(f"settings: {format_settings(settings)}")
    usage = getattr(run, "usage", None)
    if usage:
        lines.append(f"usage: {format_usage(usage)}")
    lines += [
        LIGHT,
        "PROMPT:",
        run.prompt_text.rstrip() or "(empty)",
    ]
    reasoning = getattr(run, "reasoning", "")
    if reasoning:
        lines += [LIGHT, "REASONING:", reasoning.rstrip()]
    lines += [
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


def read(data_dir: str, limit: int | None = None) -> str:
    """Whole transcript, or the last `limit` characters of it."""
    path = transcript_path(data_dir)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if limit is not None and len(text) > limit:
        return "... (earlier entries trimmed) ...\n\n" + text[-limit:]
    return text
