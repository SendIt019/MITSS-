"""Core objects for the prompt pipeline.

The pipeline is deliberately opinion-free about content. A prompt is text. An
output is text. The harness does not judge either one — it renders the exact
prompt that was sent, captures the exact output that came back, records who
produced it, and keeps enough structure that two of them can be compared.

The one piece of judgement in the system is the operator's own: a verdict on
each output, recorded so that reading an output once is not wasted work.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Verdicts an operator can record after reading an output. UNRATED is the
# default so that "not looked at yet" is distinguishable from "looked at and
# it was fine" — an important difference when scanning a matrix.
UNRATED = "unrated"
ACCURATE = "accurate"
PARTIAL = "partial"
INACCURATE = "inaccurate"

VERDICTS = (UNRATED, ACCURATE, PARTIAL, INACCURATE)

VERDICT_LABELS = {
    UNRATED: "not reviewed",
    ACCURATE: "accurate",
    PARTIAL: "partly right",
    INACCURATE: "inaccurate",
}


def is_verdict(value: str) -> bool:
    return value in VERDICTS


@dataclass
class PromptVersion:
    """One immutable revision of a prompt.

    Versions are never edited in place. Editing a prompt writes a new version,
    which is what makes a run's provenance meaningful months later.
    """

    version: int
    text: str
    note: str = ""
    created_at: str = ""

    @property
    def characters(self) -> int:
        return len(self.text)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def summary(self) -> Dict[str, Any]:
        """Metadata without the body, for listings."""
        return {
            "version": self.version,
            "note": self.note,
            "created_at": self.created_at,
            "characters": self.characters,
            "words": self.words,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary()
        data["text"] = self.text
        return data


@dataclass
class Prompt:
    """A named prompt and the full history of its revisions."""

    id: str
    name: str
    created_at: str = ""
    tags: List[str] = field(default_factory=list)
    versions: List[PromptVersion] = field(default_factory=list)

    @property
    def latest(self) -> Optional[PromptVersion]:
        return self.versions[-1] if self.versions else None

    def version(self, number: int) -> Optional[PromptVersion]:
        for candidate in self.versions:
            if candidate.version == number:
                return candidate
        return None

    def summary(self) -> Dict[str, Any]:
        latest = self.latest
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "tags": list(self.tags),
            "version_count": len(self.versions),
            "latest_version": latest.version if latest else None,
            "updated_at": latest.created_at if latest else self.created_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary()
        data["versions"] = [v.summary() for v in self.versions]
        return data


@dataclass
class Run:
    """One trip through the harness: a prompt version, a model, an output.

    `prompt_text` is frozen into the run rather than looked up later. If a
    prompt version were ever lost or moved, the run still shows exactly what
    was sent.
    """

    id: str
    prompt_id: str
    version: int
    model: str = ""
    output: str = ""
    prompt_text: str = ""
    verdict: str = UNRATED
    notes: str = ""
    created_at: str = ""
    reviewed_at: str = ""
    source: str = "paste"          # paste | provider
    duration_ms: Optional[int] = None

    @property
    def output_words(self) -> int:
        return len(self.output.split())

    def summary(self) -> Dict[str, Any]:
        """Everything except the two big text blobs."""
        return {
            "id": self.id,
            "prompt_id": self.prompt_id,
            "version": self.version,
            "model": self.model,
            "verdict": self.verdict,
            "verdict_label": VERDICT_LABELS.get(self.verdict, self.verdict),
            "notes": self.notes,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "source": self.source,
            "duration_ms": self.duration_ms,
            "output_words": self.output_words,
            "output_characters": len(self.output),
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary()
        data["output"] = self.output
        data["prompt_text"] = self.prompt_text
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Run":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        return cls(**{k: v for k, v in data.items() if k in known})


def slugify(text: str, fallback: str = "prompt") -> str:
    """Filesystem-safe, human-readable identifier."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned[:48] or fallback


def as_dict(obj: Any) -> Dict[str, Any]:
    return asdict(obj)
