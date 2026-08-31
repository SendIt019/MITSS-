"""Business logic between the HTTP layer and the pipeline core.

Nothing here imports FastAPI, so the same functions could back a command line
or a script. The core stays dependency-free; this layer only orchestrates.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from mitss.llm import HttpProvider, LLMError, ProviderUnavailable, get_provider
from pipeline import (
    NotFound, Store, build_digest, build_matrix, compare_runs, diff_text,
    digest_text,
)
from pipeline.models import UNRATED, VERDICT_LABELS, VERDICTS, is_verdict
from pipeline.render import preview as render_preview, render_prompt
from pipeline.transcript import read as read_transcript, transcript_path

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
    data["inputs_used"] = shelf.inputs_used(prompt_id)
    data["has_placeholder"] = "{input}" in (selected.text if selected else "")
    return data


# --------------------------------------------------------------------------
# input sets
# --------------------------------------------------------------------------

def list_inputs(root: Optional[str] = None) -> List[Dict[str, Any]]:
    return [i.summary() for i in store(root).list_inputs()]


def create_input(name: str, text: str, note: str = "",
                 root: Optional[str] = None) -> Dict[str, Any]:
    if not (text or "").strip():
        raise ServiceError("the input is empty")
    return store(root).create_input(name.strip() or "untitled input", text, note).to_dict()


def get_input(input_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    return _found(store(root).get_input, input_id).to_dict()


def update_input(input_id: str, name: Optional[str] = None, text: Optional[str] = None,
                 note: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    if text is not None and not text.strip():
        raise ServiceError("the input is empty")
    return _found(store(root).update_input, input_id, name, text, note).to_dict()


def delete_input(input_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    _found(store(root).delete_input, input_id)
    return {"deleted": input_id}


def upload_input(filename: str, text: str, root: Optional[str] = None) -> Dict[str, Any]:
    if not (text or "").strip():
        raise ServiceError("the uploaded file is empty")
    name = os.path.splitext(os.path.basename(filename or "input.txt"))[0]
    return create_input(name, text, f"uploaded {filename}", root=root)


def preview_prompt(prompt_id: str, version: Optional[int] = None,
                   input_id: str = "", root: Optional[str] = None) -> Dict[str, Any]:
    """What would actually be sent, given this version and this input."""
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    selected = prompt.version(version) if version is not None else prompt.latest
    if selected is None:
        raise ServiceError(f"prompt '{prompt_id}' has no version {version}", 404)

    input_text = ""
    input_name = ""
    if input_id:
        input_set = _found(shelf.get_input, input_id)
        input_text = input_set.text
        input_name = input_set.name

    payload = render_preview(selected.text, input_text)
    payload["version"] = selected.version
    payload["input_id"] = input_id
    payload["input_name"] = input_name
    return payload


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
# registered models — how a teammate hands their model to the operator
# --------------------------------------------------------------------------

# An environment variable name, not a value. Anything that does not match is
# almost certainly a pasted key, which must never reach disk.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

MODEL_FORMATS = ("openai", "raw")


def _validate_model_fields(url: Optional[str], fmt: Optional[str],
                           key_env: Optional[str]) -> None:
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ServiceError("url must start with http:// or https://")
    if fmt is not None and fmt not in MODEL_FORMATS:
        raise ServiceError(f"format must be one of: {', '.join(MODEL_FORMATS)}")
    if key_env and not _ENV_NAME.fullmatch(key_env):
        raise ServiceError(
            "key_env must be the NAME of an environment variable (like "
            "TEAM_7B_KEY) set on the machine running the backend - never "
            "the key itself. Keys are read at call time and never stored."
        )


def _model_payload(entry) -> Dict[str, Any]:
    data = entry.summary()
    # Presence only, resolved at request time. The value is never exposed.
    data["key_set"] = bool(entry.key_env and os.environ.get(entry.key_env))
    return data


def list_models(root: Optional[str] = None) -> List[Dict[str, Any]]:
    return [_model_payload(m) for m in store(root).list_models()]


def register_model(name: str, owner: str = "", url: str = "",
                   fmt: str = "openai", model: str = "", key_env: str = "",
                   notes: str = "", root: Optional[str] = None) -> Dict[str, Any]:
    if not (name or "").strip():
        raise ServiceError("the model needs a name - it is the label runs are recorded under")
    _validate_model_fields(url, fmt, key_env)
    entry = store(root).register_model(
        name.strip(), owner.strip(), url.strip(), fmt, model.strip(),
        key_env.strip(), notes,
    )
    return _model_payload(entry)


def get_model(model_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    return _model_payload(_found(store(root).get_model, model_id))


def update_model(model_id: str, owner: Optional[str] = None,
                 url: Optional[str] = None, fmt: Optional[str] = None,
                 model: Optional[str] = None, key_env: Optional[str] = None,
                 notes: Optional[str] = None,
                 root: Optional[str] = None) -> Dict[str, Any]:
    _validate_model_fields(url, fmt, key_env)
    entry = _found(store(root).update_model, model_id, owner, url, fmt,
                   model, key_env, notes)
    return _model_payload(entry)


def delete_model(model_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    _found(store(root).delete_model, model_id)
    return {"deleted": model_id}


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def record_run(prompt_id: str, version: Optional[int], model: str, output: str,
               notes: str = "", verdict: str = UNRATED, input_id: str = "",
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
                 notes, verdict, "paste", None, input_id)
    return run.to_dict()


def run_detail(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    return _found(store(root).get_run, run_id).to_dict()


def list_runs(prompt_id: Optional[str] = None, version: Optional[int] = None,
              model: Optional[str] = None, input_id: Optional[str] = None,
              verdict: Optional[str] = None,
              root: Optional[str] = None) -> List[Dict[str, Any]]:
    if verdict is not None and not is_verdict(verdict):
        raise ServiceError(f"verdict must be one of: {', '.join(VERDICTS)}")
    return [r.summary()
            for r in store(root).list_runs(prompt_id, version, model, input_id,
                                           verdict)]


def review_run(run_id: str, verdict: Optional[str] = None,
               notes: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    if verdict is not None and not is_verdict(verdict):
        raise ServiceError(f"verdict must be one of: {', '.join(VERDICTS)}")
    return _found(store(root).review_run, run_id, verdict, notes).to_dict()


def delete_run(run_id: str, root: Optional[str] = None) -> Dict[str, Any]:
    _found(store(root).delete_run, run_id)
    return {"deleted": run_id}


def _resolve_version(shelf: Store, prompt_id: str, version: Optional[int]):
    prompt = _found(shelf.get_prompt, prompt_id)
    target = version if version is not None else (prompt.latest.version if prompt.latest else None)
    if target is None:
        raise ServiceError(f"prompt '{prompt_id}' has no versions", 409)
    prompt_version = prompt.version(target)
    if prompt_version is None:
        raise ServiceError(f"prompt '{prompt_id}' has no version {target}", 404)
    return target, prompt_version


def _fetch_and_record(shelf: Store, provider, label: str, ask_for: Optional[str],
                      prompt_id: str, target: int, rendered: str,
                      input_id: str) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        # Pass the chosen model through, so the endpoint is actually asked for
        # it rather than the run merely being labelled with it.
        output = provider.complete(rendered, ask_for)
    except ProviderUnavailable as exc:
        raise ServiceError(str(exc), 409) from None
    except LLMError as exc:
        raise ServiceError(str(exc), 502) from None
    elapsed = int((time.monotonic() - started) * 1000)

    run = shelf.create_run(prompt_id, target, label, output, source="provider",
                           duration_ms=elapsed, input_id=input_id)
    return run.to_dict()


def generate_run(prompt_id: str, version: Optional[int] = None, model: str = "",
                 input_id: str = "", model_id: str = "",
                 root: Optional[str] = None) -> Dict[str, Any]:
    """Send the prompt to a model and record what comes back.

    `model_id` names a registered model, whose own endpoint is called and
    whose name labels the run. Without it, the environment-configured provider
    is used; with the default manual provider that reports a conflict, which
    is the expected path: copy the prompt, run it yourself, paste it back.
    """
    shelf = store(root)
    target, prompt_version = _resolve_version(shelf, prompt_id, version)

    input_text = ""
    if input_id:
        input_text = _found(shelf.get_input, input_id).text
    rendered, _ = render_prompt(prompt_version.text, input_text)

    if model_id:
        entry = _found(shelf.get_model, model_id)
        if not entry.callable:
            raise ServiceError(
                f"'{entry.name}' is registered paste-only (no url) - copy the "
                "rendered prompt, run it through that model, and paste the "
                "output back under its name", 409,
            )
        provider = HttpProvider(url=entry.url, fmt=entry.format,
                                model=entry.model or entry.name,
                                key_env=entry.key_env or None)
        return _fetch_and_record(shelf, provider, entry.name, None,
                                 prompt_id, target, rendered, input_id)

    provider = get_provider()
    described = provider.describe()
    label = model or described.get("model") or provider.name
    return _fetch_and_record(shelf, provider, label, model or None,
                             prompt_id, target, rendered, input_id)


def batch_generate(prompt_id: str, version: Optional[int] = None,
                   input_id: str = "", model_ids: Optional[List[str]] = None,
                   root: Optional[str] = None) -> Dict[str, Any]:
    """One prompt version across several registered models in one action.

    With no `model_ids`, every callable registered model is asked. One model
    failing does not stop the rest; each result says what happened, and every
    successful call is recorded as an ordinary run.
    """
    shelf = store(root)
    if model_ids:
        entries = [_found(shelf.get_model, mid) for mid in model_ids]
    else:
        entries = [m for m in shelf.list_models() if m.callable]
    if not entries:
        raise ServiceError(
            "no callable registered models - register one with a url on the "
            "Models tab first", 409,
        )

    results = []
    for entry in entries:
        try:
            run = generate_run(prompt_id, version, input_id=input_id,
                               model_id=entry.id, root=root)
            results.append({"model_id": entry.id, "model": entry.name,
                            "ok": True, "run_id": run["id"]})
        except ServiceError as exc:
            results.append({"model_id": entry.id, "model": entry.name,
                            "ok": False, "error": exc.message})
    return {
        "results": results,
        "recorded": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
    }


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def matrix(prompt_id: str, input_id: Optional[str] = None,
           root: Optional[str] = None) -> Dict[str, Any]:
    """Versions against models, optionally narrowed to one input set.

    `input_id=None` aggregates every input, which is fine for a coverage
    overview but is not a like-for-like comparison. Pass an input id to compare
    wording fairly.
    """
    shelf = store(root)
    prompt = _found(shelf.get_prompt, prompt_id)
    runs = shelf.list_runs(prompt_id=prompt_id, input_id=input_id)
    versions = [v.version for v in prompt.versions]
    payload = build_matrix(runs, versions)
    payload["prompt"] = prompt.summary()
    payload["version_notes"] = {v.version: v.note for v in prompt.versions}
    payload["input_id"] = input_id
    payload["available_inputs"] = shelf.inputs_used(prompt_id)
    payload["like_for_like"] = input_id is not None or len(payload["inputs_in_view"]) <= 1
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


def digest(root: Optional[str] = None) -> Dict[str, Any]:
    """Everything recorded, rolled up by prompt, version and model."""
    shelf = store(root)
    return build_digest(shelf.list_prompts(), shelf.list_runs(),
                        shelf.list_models())


def digest_as_text(root: Optional[str] = None) -> str:
    return digest_text(digest(root))


def verdict_options() -> List[Dict[str, str]]:
    return [{"value": v, "label": VERDICT_LABELS[v]} for v in VERDICTS]


def activity(limit: int = 50, root: Optional[str] = None) -> List[Dict[str, Any]]:
    return store(root).read_events(limit=limit)


def transcript(limit: Optional[int] = None, root: Optional[str] = None) -> str:
    """The rolling plain-text transcript of every run."""
    shelf = store(root)
    text = read_transcript(shelf.data_dir, limit)
    return text or "No runs recorded yet.\n"


def transcript_location(root: Optional[str] = None) -> str:
    return transcript_path(store(root).data_dir)
