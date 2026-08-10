"""FastAPI application exposing the MITSS harness to the React frontend.

Run it with:

    cd backend
    uvicorn app.main:app --reload --port 8000

The HTTP layer is deliberately thin. Every route calls into app.service, which
calls the dependency-free core. Business rules live there, not here.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from . import service
from .service import ServiceError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB is far more than any plan needs

app = FastAPI(
    title="MITSS",
    version="0.2.0",
    description="Input/output harness for language-model scheduling runs.",
)

# The dev frontend runs on a different port, so it needs explicit permission.
# Override with MITSS_CORS_ORIGINS="http://host:port,http://other" if needed.
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

class PlanReply(BaseModel):
    raw: str = Field(..., description="The model's reply containing the structured plan")


class ScheduleReply(BaseModel):
    raw: str = Field(..., description="The model's reply containing the schedule")
    model: str = Field("", description="Which model produced this reply")
    note: str = Field("", description="Optional free-text label for the run")


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------

def _guard(call, *args, **kwargs):
    """Translate ServiceError into an HTTP response with the right status."""
    try:
        return call(*args, **kwargs)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/llm")
def llm_status():
    """What the model harness is currently configured to do."""
    return service.llm_status()


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)):
    """Upload a .txt plan. Parses deterministically, falls back to a model."""
    name = file.filename or "upload.txt"
    if not name.lower().endswith((".txt", ".text", ".md")):
        raise HTTPException(400, "please upload a .txt file")

    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 2 MB")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "file must be UTF-8 text") from None

    return _guard(service.upload_text, name, text)


@app.post("/api/runs/{run_id}/plan")
def attach_plan(run_id: str, body: PlanReply):
    """Attach a model-structured plan to a run whose text could not be parsed."""
    return _guard(service.attach_plan, run_id, body.raw)


@app.post("/api/runs/{run_id}/ingest")
def ingest(run_id: str, body: ScheduleReply):
    """Validate and constraint-check a schedule the model returned."""
    return _guard(service.ingest_schedule, run_id, body.raw, body.model, body.note)


@app.post("/api/runs/{run_id}/solve")
def solve(run_id: str):
    """Ask the configured provider directly. 409 when set to manual paste."""
    return _guard(service.solve_with_model, run_id)


@app.get("/api/runs")
def runs():
    return {"runs": service.list_runs()}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    return _guard(service.run_detail, run_id)


@app.get("/api/runs/{run_id}/packet", response_class=PlainTextResponse)
def packet(run_id: str):
    detail = _guard(service.run_detail, run_id)
    text = detail.get("packet") or detail.get("structuring_packet")
    if not text:
        raise HTTPException(409, "this run has no packet yet")
    return PlainTextResponse(text, media_type="text/markdown")


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: str):
    text = _guard(service.run_csv, run_id)
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'},
    )


@app.get("/api/diff")
def diff(a: str = Query(...), b: str = Query(...)):
    return _guard(service.diff_runs, a, b)
