"""MITSS prompt pipeline.

You write a prompt, take it to your model, and bring the output back. The
pipeline renders the exact prompt, captures the exact output, records which
model produced it and what you concluded on reading it, and keeps enough
structure to compare any two results.

It does not judge output. That is the operator's job, and the verdict field is
where that judgement is stored so it is not lost.

Standard library only. No network access; a model is reached, if at all,
through the provider harness in mitss.llm.
"""

from .compare import build_matrix, compare_runs, diff_text
from .models import (
    ACCURATE,
    INACCURATE,
    PARTIAL,
    UNRATED,
    VERDICTS,
    VERDICT_LABELS,
    Prompt,
    PromptVersion,
    Run,
    is_verdict,
    slugify,
)
from .store import NotFound, Store

__all__ = [
    "ACCURATE",
    "INACCURATE",
    "NotFound",
    "PARTIAL",
    "Prompt",
    "PromptVersion",
    "Run",
    "Store",
    "UNRATED",
    "VERDICTS",
    "VERDICT_LABELS",
    "build_matrix",
    "compare_runs",
    "diff_text",
    "is_verdict",
    "slugify",
]
