# MITSS

Upload a plain-text scheduling problem, hand the generated packet to your own
language model, paste its answer back, and have that answer **checked** rather
than trusted.

```
frontend/   React user interface (Vite)
backend/    Python: FastAPI application over a dependency-free core
```

MITSS does not schedule anything itself. Your model does the scheduling; MITSS
parses the input, builds the prompt, validates the reply against a schema,
enforces the hard constraints, renders the result, and keeps an append-only
record of every run so answers can be compared over time.

## Quick start

```bash
./dev.sh
```

That starts the API on `http://127.0.0.1:8000` and the interface on
`http://127.0.0.1:5173`, installing frontend dependencies on first run.

Manually, if you prefer:

```bash
cd backend  && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev
```

Requirements: Python 3.9 or newer, Node 18 or newer.

## The cycle

Upload a `.txt` file. The backend tries the structured grammar first; if the
file does not match, the raw text is handed to your model to structure, and the
structured result is validated before it is accepted. Either way you end up with
a plan and a packet.

Copy the packet into your model. Paste the reply back, record which model
answered, and press check. You get a timeline, an assignment table, summary
tiles, a CSV export, and a list of every schema or constraint problem found.

## Input format

The deterministic grammar. Keywords are case-insensitive, `#` starts a comment,
fields are separated by `|`:

```text
SESSION:   demo-001
DOMAIN:    field-ops
HORIZON:   2026-08-11 08:00 -> 18:00
GRID:      15min
OBJECTIVE: finish as early as possible
NOTE:      weather window closes at 1600

RESOURCE: alpha | Team Alpha | cap 1
RESOURCE: bravo | Team Bravo | cap 2 | available 12:00 -> 18:00

TASK: t1 | Site survey     | 120min
TASK: t2 | Equipment setup | 1h30m | after t1
TASK: t3 | Calibration     | 1h    | after t2 | needs alpha | by 16:00
TASK: t4 | Teardown        | 45min | after t3 | not before 14:00 | priority 1
```

Modifiers, in any order after the duration: `after A, B` (dependencies),
`needs A, B` (eligible resources), `by <time>` (deadline), `not before <time>`
(earliest start), `priority N`, `cap N` (resources), and repeatable
`available X -> Y` windows.

Times are `HH:MM` (borrowing the horizon's date) or a full `YYYY-MM-DD HH:MM`.
Durations accept `90`, `90min`, `2h`, `1h30m`. Anything the parser cannot read
is reported with its line number rather than guessed at.

Free-form prose is fine too — it simply takes the model-structuring path.

## Plugging in your model

The default provider is `manual`: MITSS gives you a packet and takes a reply,
and never calls anything. To have the backend call your model directly, copy
`backend/.env.example` to `backend/.env` and set the endpoint:

```bash
MITSS_LLM_PROVIDER=http
MITSS_LLM_URL=http://127.0.0.1:8080/v1/chat/completions
MITSS_LLM_FORMAT=openai        # or "raw" for {"prompt": ...}
MITSS_LLM_MODEL=my-custom-model
```

The `openai` shape works with llama.cpp, vLLM, Ollama and LM Studio unchanged.
For anything else, subclass `LLMProvider` in `backend/mitss/llm.py` and call
`register_provider("myname", MyProvider)` — nothing else in the codebase needs
to change.

Credentials are read from the environment at call time, sent once in the request
header, and never logged, returned by the API, or written into a run folder.
`GET /api/llm` reports whether a key is present, never its value.

## What gets checked

Structural validation first: types, required fields, parseable timestamps,
intervals that end after they start. Then the hard constraints — every task
scheduled exactly once or explicitly listed as unscheduled with a reason; every
id real; each block exactly as long as its task requires; nothing starting
before a dependency finishes; no resource running more concurrent work than its
capacity allows; everything inside availability windows and the horizon;
earliest-start and deadline respected. Start times off the time grid are
warnings, not errors.

Plans are also checked for impossibility before a model ever sees them:
dependency cycles, tasks longer than the horizon, deadlines at or before their
own earliest start, references to ids that do not exist.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness |
| `GET` | `/api/llm` | how the model harness is configured |
| `POST` | `/api/uploads` | upload a `.txt`, parse or fall back |
| `POST` | `/api/runs/{id}/plan` | attach a model-structured plan |
| `POST` | `/api/runs/{id}/ingest` | check a returned schedule |
| `POST` | `/api/runs/{id}/solve` | call the configured model directly |
| `GET` | `/api/runs` | list runs |
| `GET` | `/api/runs/{id}` | full run detail |
| `GET` | `/api/runs/{id}/packet` | the packet as markdown |
| `GET` | `/api/runs/{id}/export.csv` | schedule as CSV |
| `GET` | `/api/diff?a=&b=` | compare two runs |

Interactive documentation is at `http://127.0.0.1:8000/docs` while the backend
is running.

## What a run folder holds

```
backend/runs/20260811-084500-demo-001/
  source.txt        the uploaded file, verbatim
  plan.json         the parsed plan (runs stay reproducible)
  packet.md         what was sent to the model
  output.raw.md     what came back, verbatim
  schedule.json     the parsed schedule
  summary.json      makespan, counts, per-resource utilization
  schedule.csv      spreadsheet export
  issues.json       every error and warning found
  meta.json         which model answered, plus notes and timestamps
```

`backend/runs/index.jsonl` is an append-only log — one line per event, never
rewritten. Run folders can be deleted without losing the history.

## Command line

The core still ships a command line for use without the interface:

```bash
cd backend
python -m mitss new my-session
python -m mitss stage
python -m mitss ingest --model my-custom-model
python -m mitss report
python -m mitss diff RUN_A RUN_B
```

## Tests

```bash
cd backend && python -m unittest discover tests
cd frontend && npm run build
```

96 backend tests: the core validation and constraint suite, the text grammar
including line-numbered failures, the model harness (exercised against a real
local HTTP server, including that an API key never reaches an error message),
and the full HTTP surface.

## Design notes

The core package under `backend/mitss/` has no third-party dependencies and no
network access. FastAPI and uvicorn are required only by `backend/app/`, so the
scheduling logic stays portable and testable on its own.

The scheduling model is domain-agnostic: tasks with durations, dependencies and
eligibility, placed onto resources with capacity and availability. Shift
rosters, mission timelines and asset booking all express in those primitives.

Validation returns lists of issues rather than raising, so one pass reports every
problem instead of the first. Errors mean illegal; warnings mean legal but
suspect. See `DECISIONS.md` for the full decision log.
