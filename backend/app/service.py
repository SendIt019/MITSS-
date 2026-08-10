"""Business logic between the HTTP layer and the pipeline core.

Nothing here imports FastAPI, so the same functions could back a command line
or a script. The core stays dependency-free; this layer only orchestrates.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from mitss.llm import LLMError, ProviderUnavailable, get_provider
from pipeline import NotFound, Store, build_matrix, compare_runs, diff_text
from pipeline.models import UNRATED, VERDICT_LABELS, VERDICTS, is_verdict

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServiceError(Exception):
    """Anything the interface should report as a clean 4xx or 5xx."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def store(root: Optional[str] = None) -> Store:
    return Store(root or os.environ.get("MITSS_ROOT", BACKEND_ROOT))


def _found(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except NotFound as exc:
        raise ServiceError(str(exc), 404) from None


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

def list_prompts(root: Optional[str] = None) -> List[Dict[str, Any]]:
    return [p.summary() for p in store(root).list_prompts()]


def create_prompt(name: str, text: str, note: str = "",
                  tags: Optional[List[str]] = None,
                  root: Optional[str] = None) -> Dict[str, Any]:
    if not (text or "").strip():
        raise ServiceError("the prompt is empty")
    prompt = store(root).create_prompt(name.strip() or "untitled", text, note, tags)
    return prompt_detail(prompt.id, root=root)


def prompt_detail(prompt_id: str, version: Optional[int] = None,
                  root: Optional[str] = None) -> Dict[str, Any]:
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)

    selected = prompt.version(version) if version is not None else prompt.latest
    if version is not None and selected is None:
        raise ServiceError(f"prompt '{prompt_id}' has no version {version}", 404)

    runs = shelf.list_runs(prompt_id=prompt_id)
    data = prompt.to_dict()
    data["selected_version"] = selected.to_dict() if selected else None
    data["run_count"] = len(runs)
    data["models"] = shelf.models_used(prompt_id)
    return data


def add_version(prompt_id: str, text: str, note: str = "",
                root: Optional[str] = None) -> Dict[str, Any]:
    if not (text or "").strip():
        raise ServiceError("the prompt is empty")
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)

    # Saving an unchanged prompt would create a version that means nothing and
    # clutter every later comparison.
    latest = prompt.latest
    if latest is not None and latest.text == text:
        raise ServiceError("this is identical to the current version", 409)

    shelf.add_version(prompt_id, text, note)
    return prompt_detail(prompt_id, root=root)


def rename_prompt(prompt_id: str, name: str, root: Optional[str] = None) -> Dict[str, Any]:
    if not (name or "").strip():
        raise ServiceError("name cannot be empty")
    shelf = store(root)
    _found(shelf.rename_prompt, prompt_id, name.strip())
    return prompt_detail(prompt_id, root=root)


def upload_prompt(filename: str, text: str, prompt_id: Optional[str] = None,
                  note: str = "", root: Optional[str] = None) -> Dict[str, Any]:
    """A .txt upload either starts a new prompt or adds a version to one."""
    if not (text or "").strip():
        raise ServiceError("the uploaded file is empty")

    if prompt_id:
        return add_version(prompt_id, text, note or f"uploaded {filename}", root=root)

    name = os.path.splitext(os.path.basename(filename or "prompt.txt"))[0]
    return create_prompt(name, text, note or f"uploaded {filename}", root=root)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def record_run(prompt_id: str, version: Optional[int], model: str, output: str,
               notes: str = "", verdict: str = UNRATED,
               root: Optional[str] = None) -> Dict[str, Any]:
    if not (output or "").strip():
        raise ServiceError("the output is empty")
    if not is_verdict(verdict):
        raise ServiceError(f"verdict must be one of: {', '.join(VERDICTS)}")

    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    target = version if version is not None else (prompt.latest.version if prompt.latest else None)
    if target is None:
        raise ServiceError(f"prompt '{prompt_id}' has no versions", 409)

    run = _found(shelf.create_run, prompt_id, target, model.strip(), output,
                 notes, verdict)
    return run.to_dict()


def run_detail(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    return _found(store(root).get_run, run_id).to_dict()


def list_runs(prompt_id: Optional[str] = None, version: Optional[int] = None,
              model: Optional[str] = None, root: Optional[str] = None) -> List[Dict[str, Any]]:
    return [r.summary() for r in store(root).list_runs(prompt_id, version, model)]


def review_run(run_id: str, verdict: Optional[str] = None,
               notes: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    if verdict is not None and not is_verdict(verdict):
        raise ServiceError(f"verdict must be one of: {', '.join(VERDICTS)}")
    return _found(store(root).review_run, run_id, verdict, notes).to_dict()


def delete_run(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    _found(store(root).delete_run, run_id)
    return {"deleted": run_id}


def generate_run(prompt_id: str, version: Optional[int] = None, model: str = "",
                 root: Optional[str] = None) -> Dict[str, Any]:
    """Send the prompt to the configured provider and record what comes back.

    With the default manual provider this reports a conflict, which is the
    expected path: copy the prompt, run it yourself, paste the output back.
    """
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    target = version if version is not None else (prompt.latest.version if prompt.latest else None)
    if target is None:
        raise ServiceError(f"prompt '{prompt_id}' has no versions", 409)
    prompt_version = prompt.version(target)
    if prompt_version is None:
        raise ServiceError(f"prompt '{prompt_id}' has no version {target}", 404)

    provider = get_provider()
    started = time.monotonic()
    try:
        output = provider.complete(prompt_version.text)
    except ProviderUnavailable as exc:
        raise ServiceError(str(exc), 409) from None
    except LLMError as exc:
        raise ServiceError(str(exc), 502) from None
    elapsed = int((time.monotonic() - started) * 1000)

    label = model or provider.describe().get("model") or provider.name
    run = shelf.create_run(prompt_id, target, label, output, source="provider",
                           duration_ms=elapsed)
    return run.to_dict()


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def matrix(prompt_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    runs = shelf.list_runs(prompt_id=prompt_id)
    versions = [v.version for v in prompt.versions]
    payload = build_matrix(runs, versions)
    payload["prompt"] = prompt.summary()
    payload["version_notes"] = {v.version: v.note for v in prompt.versions}
    return payload


def compare(run_a: str, run_b: str, root: Optional[str] = None) -> Dict[str, Any]:
    shelf = store(root)
    left = _found(shelf.get_run, run_a)
    right = _found(shelf.get_run, run_b)
    payload = compare_runs(left, right)
    payload["left"]["output"] = left.output
    payload["right"]["output"] = right.output
    return payload


def compare_versions(prompt_id: str, a: int, b: int,
                     root: Optional[str] = None) -> Dict[str, Any]:
    """Diff two revisions of the prompt itself."""
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    left = prompt.version(a)
    right = prompt.version(b)
    if left is None or right is None:
        raise ServiceError("one of those versions does not exist", 404)
    return {
        "left": left.summary(),
        "right": right.summary(),
        "diff": diff_text(left.text, right.text),
    }


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------

def llm_status() -> Dict[str, Any]:
    return get_provider().describe()


def verdict_options() -> List[Dict[str, str]]:
    return [{"value": v, "label": VERDICT_LABELS[v]} for v in VERDICTS]


def activity(limit: int = 50, root: Optional[str] = None) -> List[Dict[str, Any]]:
    return store(root).read_events(limit=limit)
