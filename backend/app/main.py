"""FastAPI application for the MITSS prompt pipeline.

Run it with:

    cd backend
    uvicorn app.main:app --reload --port 8000

The HTTP layer is thin on purpose: every route calls app.service, which calls
the dependency-free pipeline core.
"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import service
from .service import ServiceError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024

app = FastAPI(
    title="MITSS",
    version="0.4.0",
    description="Prompt engineering pipeline: render a prompt, capture the "
                "output, record what you concluded, compare across versions "
                "and models.",
)

_origins = os.environ.get(
    "MITSS_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------

class NewPrompt(BaseModel):
    name: str = Field("", description="Human-readable name")
    text: str = Field(..., description="The prompt itself")
    note: str = Field("", description="Why this version exists")
    tags: List[str] = Field(default_factory=list)


class NewVersion(BaseModel):
    text: str = Field(..., description="The revised prompt")
    note: str = Field("", description="What changed and why")


class Rename(BaseModel):
    name: str


class NewInput(BaseModel):
    name: str = Field("", description="Human-readable name for this input set")
    text: str = Field(..., description="The material the prompt is applied to")
    note: str = ""


class EditInput(BaseModel):
    name: Optional[str] = None
    text: Optional[str] = None
    note: Optional[str] = None


class NewRun(BaseModel):
    prompt_id: str
    version: Optional[int] = Field(None, description="Defaults to the latest version")
    model: str = Field("", description="Which model produced this output")
    output: str = Field(..., description="The model's output, verbatim")
    notes: str = ""
    verdict: str = Field("unrated", description="unrated | accurate | partial | inaccurate")
    input_id: str = Field("", description="Which input set this was run against")


class Review(BaseModel):
    verdict: Optional[str] = None
    notes: Optional[str] = None


class Generate(BaseModel):
    prompt_id: str
    version: Optional[int] = None
    model: str = ""
    input_id: str = ""
    model_id: str = Field("", description="A registered model to call instead "
                                          "of the environment-configured provider")


class Batch(BaseModel):
    prompt_id: str
    version: Optional[int] = None
    input_id: str = ""
    model_ids: List[str] = Field(default_factory=list,
                                 description="Registered models to ask; empty "
                                             "means every callable one")


class NewModel(BaseModel):
    name: str = Field(..., description="Label runs are recorded under")
    owner: str = Field("", description="Whose model this is")
    url: str = Field("", description="Endpoint; empty registers it paste-only")
    format: str = Field("openai", description="openai | raw")
    model: str = Field("", description="Name sent in the request body; defaults to the label")
    key_env: str = Field("", description="NAME of the env var holding the key - never the key")
    notes: str = ""


class EditModel(BaseModel):
    owner: Optional[str] = None
    url: Optional[str] = None
    format: Optional[str] = None
    model: Optional[str] = None
    key_env: Optional[str] = None
    notes: Optional[str] = None


def _guard(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/llm")
def llm_status():
    """How the model harness is configured. Never exposes a key."""
    return service.llm_status()


@app.get("/api/verdicts")
def verdicts():
    return {"verdicts": service.verdict_options()}


@app.get("/api/activity")
def activity(limit: int = Query(50, ge=1, le=500)):
    return {"events": service.activity(limit)}


@app.get("/api/transcript", response_class=PlainTextResponse)
def transcript(limit: Optional[int] = Query(None, ge=1),
               download: bool = Query(False)):
    """The rolling plain-text transcript of every recorded run."""
    text = service.transcript(limit)
    headers = ({"Content-Disposition": 'attachment; filename="mitss-transcript.txt"'}
               if download else {})
    return PlainTextResponse(text, media_type="text/plain", headers=headers)


@app.get("/api/transcript/location")
def transcript_location():
    """Where the transcript lives on disk, for opening it outside the app."""
    return {"path": service.transcript_location()}


@app.get("/api/digest")
def digest(format: str = Query("json", pattern="^(json|text)$"),
           download: bool = Query(False)):
    """Everything recorded, rolled up by prompt, version and model.

    `format=text` returns the same digest as a plain-text page — readable
    without the tool, and compact enough to paste into a model as context.
    """
    if format == "text":
        headers = ({"Content-Disposition": 'attachment; filename="mitss-digest.txt"'}
                   if download else {})
        return PlainTextResponse(service.digest_as_text(),
                                 media_type="text/plain", headers=headers)
    return service.digest()


# --------------------------------------------------------------------------
# registered models
# --------------------------------------------------------------------------

@app.get("/api/models")
def list_models():
    """Models the team has registered. Never includes a key."""
    return {"models": service.list_models()}


@app.post("/api/models")
def register_model(body: NewModel):
    """Register a teammate's model: connection details, never credentials."""
    return _guard(service.register_model, body.name, body.owner, body.url,
                  body.format, body.model, body.key_env, body.notes)


@app.get("/api/models/{model_id}")
def get_model(model_id: str):
    return _guard(service.get_model, model_id)


@app.patch("/api/models/{model_id}")
def update_model(model_id: str, body: EditModel):
    """Connection details are editable; the name is not — runs carry it."""
    return _guard(service.update_model, model_id, body.owner, body.url,
                  body.format, body.model, body.key_env, body.notes)


@app.delete("/api/models/{model_id}")
def delete_model(model_id: str):
    """Removes the registration only; recorded runs keep its name."""
    return _guard(service.delete_model, model_id)


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

@app.get("/api/prompts")
def list_prompts():
    return {"prompts": service.list_prompts()}


@app.post("/api/prompts")
def create_prompt(body: NewPrompt):
    return _guard(service.create_prompt, body.name, body.text, body.note, body.tags)


@app.get("/api/prompts/{prompt_id}")
def prompt_detail(prompt_id: str, version: Optional[int] = None):
    return _guard(service.prompt_detail, prompt_id, version)


@app.post("/api/prompts/{prompt_id}/versions")
def add_version(prompt_id: str, body: NewVersion):
    return _guard(service.add_version, prompt_id, body.text, body.note)


@app.patch("/api/prompts/{prompt_id}")
def rename_prompt(prompt_id: str, body: Rename):
    return _guard(service.rename_prompt, prompt_id, body.name)


@app.get("/api/prompts/{prompt_id}/versions/{version}/text", response_class=PlainTextResponse)
def version_text(prompt_id: str, version: int):
    detail = _guard(service.prompt_detail, prompt_id, version)
    return PlainTextResponse(detail["selected_version"]["text"], media_type="text/plain")


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...), prompt_id: str = Form(""),
                 note: str = Form("")):
    """Upload a .txt prompt: a new prompt, or a new version of an existing one."""
    name = file.filename or "prompt.txt"
    if not name.lower().endswith((".txt", ".text", ".md", ".prompt")):
        raise HTTPException(400, "please upload a .txt file")

    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 2 MB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "file must be UTF-8 text") from None

    return _guard(service.upload_prompt, name, text, prompt_id or None, note)


# --------------------------------------------------------------------------
# input sets
# --------------------------------------------------------------------------

@app.get("/api/inputs")
def list_inputs():
    """The reusable library of material prompts get applied to."""
    return {"inputs": service.list_inputs()}


@app.post("/api/inputs")
def create_input(body: NewInput):
    return _guard(service.create_input, body.name, body.text, body.note)


@app.get("/api/inputs/{input_id}")
def get_input(input_id: str):
    return _guard(service.get_input, input_id)


@app.patch("/api/inputs/{input_id}")
def update_input(input_id: str, body: EditInput):
    """Inputs are editable; past runs froze the text they used."""
    return _guard(service.update_input, input_id, body.name, body.text, body.note)


@app.delete("/api/inputs/{input_id}")
def delete_input(input_id: str):
    return _guard(service.delete_input, input_id)


@app.post("/api/inputs/upload")
async def upload_input(file: UploadFile = File(...)):
    name = file.filename or "input.txt"
    if not name.lower().endswith((".txt", ".text", ".md")):
        raise HTTPException(400, "please upload a .txt file")
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 2 MB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "file must be UTF-8 text") from None
    return _guard(service.upload_input, name, text)


@app.get("/api/prompts/{prompt_id}/preview")
def preview(prompt_id: str, version: Optional[int] = None, input_id: str = ""):
    """Exactly what would be sent for this version and input."""
    return _guard(service.preview_prompt, prompt_id, version, input_id)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

@app.post("/api/runs")
def record_run(body: NewRun):
    """Record an output you got from your model."""
    return _guard(service.record_run, body.prompt_id, body.version, body.model,
                  body.output, body.notes, body.verdict, body.input_id)


@app.get("/api/runs")
def list_runs(prompt_id: Optional[str] = None, version: Optional[int] = None,
              model: Optional[str] = None, input_id: Optional[str] = None,
              verdict: Optional[str] = None):
    """`verdict=unrated` is the review queue: outputs nobody has read yet."""
    return {"runs": _guard(service.list_runs, prompt_id, version, model,
                           input_id, verdict)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    return _guard(service.run_detail, run_id)


@app.patch("/api/runs/{run_id}")
def review_run(run_id: str, body: Review):
    """Record your verdict after reading the output."""
    return _guard(service.review_run, run_id, body.verdict, body.notes)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    return _guard(service.delete_run, run_id)


@app.post("/api/generate")
def generate(body: Generate):
    """Call the configured provider or a registered model. 409 on paste-only."""
    return _guard(service.generate_run, body.prompt_id, body.version, body.model,
                  body.input_id, body.model_id)


@app.post("/api/batch")
def batch(body: Batch):
    """Run one prompt version across several registered models in one action."""
    return _guard(service.batch_generate, body.prompt_id, body.version,
                  body.input_id, body.model_ids or None)


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

@app.get("/api/prompts/{prompt_id}/matrix")
def matrix(prompt_id: str, input_id: Optional[str] = None):
    """Prompt versions down the side, models across the top.

    Pass input_id to narrow to one input set, which is the only way the
    comparison is like-for-like.
    """
    return _guard(service.matrix, prompt_id, input_id)


@app.get("/api/compare")
def compare(a: str = Query(...), b: str = Query(...)):
    """Side-by-side word-level diff of two recorded outputs."""
    return _guard(service.compare, a, b)


@app.get("/api/prompts/{prompt_id}/compare-versions")
def compare_versions(prompt_id: str, a: int = Query(...), b: int = Query(...)):
    """Diff two revisions of the prompt itself."""
    return _guard(service.compare_versions, prompt_id, a, b)
