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
class InputSet:
    """Named material a prompt gets applied to.

    Kept separate from the prompt so that version history tracks wording rather
    than whichever document happened to be pasted in that day. Reusable, so the
    same test case can be run against every version.
    """

    id: str
    name: str
    text: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def characters(self) -> int:
        return len(self.text)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "characters": self.characters,
            "words": self.words,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.summary()
        data["text"] = self.text
        return data


@dataclass
class Run:
    """One trip through the harness: a prompt version, an input, a model, an output.

    Three texts are frozen into the run rather than looked up later:
    `template_text` (the prompt version's wording), `input_text` (the material)
    and `prompt_text` (what those two rendered into, which is what was actually
    sent). Editing a prompt or an input afterwards cannot rewrite history.
    """

    id: str
    prompt_id: str
    version: int
    model: str = ""
    output: str = ""
    prompt_text: str = ""          # the rendered prompt, exactly as sent
    template_text: str = ""        # the prompt version it came from
    input_id: str = ""
    input_name: str = ""
    input_text: str = ""
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
        """Everything except the big text blobs."""
        return {
            "id": self.id,
            "prompt_id": self.prompt_id,
            "version": self.version,
            "model": self.model,
            "input_id": self.input_id,
            "input_name": self.input_name,
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
        data["template_text"] = self.template_text
        data["input_text"] = self.input_text
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Run":
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ModelEntry:
    """A model someone on the team has registered with the harness.

    A registration is connection details, not credentials: `key_env` is the
    NAME of an environment variable the operator sets on the machine running
    the backend. The key itself is never written to disk, and this dataclass
    has nowhere to put one.

    An entry with no `url` is paste-only: it exists so runs are labelled
    consistently and the matrix has a column, but the operator carries the
    prompt to the model by hand.
    """

    id: str
    name: str                     # the label runs are recorded under
    owner: str = ""               # whose model this is
    url: str = ""                 # empty means paste-only
    format: str = "openai"        # openai | raw, same as the http provider
    model: str = ""               # name sent in the request body; defaults to name
    key_env: str = ""             # env var name holding the key, never the key
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def callable(self) -> bool:
        """True if the backend can reach this model itself."""
        return bool(self.url)

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "owner": self.owner,
            "url": self.url,
            "format": self.format,
            "model": self.model or self.name,
            "key_env": self.key_env,
            "notes": self.notes,
            "callable": self.callable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def slugify(text: str, fallback: str = "prompt") -> str:
    """Filesystem-safe, human-readable identifier."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned[:48] or fallback


def as_dict(obj: Any) -> Dict[str, Any]:
    return asdict(obj)
