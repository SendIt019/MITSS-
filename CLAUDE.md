# Working on MITSS

Read this before changing anything.

## What this is

A prompt engineering pipeline. A prompt goes in, Jake runs it through his own
model, the output comes back, and the harness records what happened so results
can be compared across prompt versions and models.

It is **capture-only by design**. The harness does not judge output — Jake does,
by reading it, and records a verdict. Do not add automated scoring, assertions,
or schema checks on model output unless he explicitly asks. That decision is in
`DECISIONS.md` and was made deliberately.

## Invariants — breaking these breaks the product

**Prompt versions are immutable.** Editing writes a new version. Never mutate
`v1.txt`. The whole value of the matrix depends on an old result still
reflecting the old prompt.

**Runs freeze three texts.** `template.txt` (the version's wording),
`input.txt` (the material), `prompt.txt` (what they rendered into — what was
actually sent). Editing or deleting an input later must never change what a
past run shows. There are tests on this.

**What you copy is what runs.** The interface copies the *rendered* prompt, not
the template. Any change that lets those diverge is a bug, even if nothing
errors.

**A model label must match what the endpoint was asked for.** `complete()`
takes a model override and it must reach the request body. Labelling a run with
a model that was never sent makes every comparison a lie. This was a real bug —
see the 2026-08-15 entry in `DECISIONS.md`.

**The pipeline core has no third-party dependencies.** `backend/pipeline/` and
`backend/mitss/` import stdlib only. FastAPI and uvicorn belong to
`backend/app/`. Keep that boundary.

**Append-only means append-only.** `data/index.jsonl` and `data/transcript.txt`
are never rewritten. A verdict set after a run appends a new line; it does not
edit the block above it.

## Conventions

- `DECISIONS.md` is append-only with timestamps. Add an entry for any decision
  worth explaining later, including reversals — say what changed and why.
  Never edit or delete an existing entry.
- Never touch credentials or tokens. Model keys come from the environment at
  call time and must never be logged, returned by an API response, or written
  into stored data. There is a test asserting a key does not leak into an error
  message; keep it passing.
- Run the tests before committing: `cd backend && python -m unittest discover tests`.
  All of them, not the ones you think you touched.
- Prefer fixing the code over loosening a test. If a test is wrong, say so
  explicitly and explain why.

## Verify, do not assert

This codebase has already been bitten by documentation describing a feature
that was never built (`SANDBOX.md` documented a model dropdown, an environment
variable, and an API field, none of which existed). Configuring the app per
those instructions silently did the wrong thing.

So: **do not write documentation for behaviour you have not run.** If you claim
an endpoint returns something, curl it. If you claim the interface shows
something, start it and look. If you add an environment variable, prove it is
read. A verification block that actually executes beats a paragraph that
sounds right.

## Running it

```bash
source .venv/bin/activate     # create with python3 -m venv .venv if missing
./dev.sh                      # backend on :8000, interface on :5173
```

Both must be running; the interface is useless without the backend. Data lives
in `backend/data/` as plain files and is gitignored.

To point it at a model, copy `backend/.env.example` to `backend/.env`. See
`SANDBOX.md` for the variable table and an end-to-end check.

## Layout

```
frontend/           React (Vite). App.jsx is the whole interface.
backend/app/        FastAPI. Thin — routes call service.py, which calls the core.
backend/pipeline/   The product: prompts, versions, inputs, runs, matrix, diffing.
backend/mitss/      The model provider harness, plus a scheduling example kept
                    from an earlier design. The pipeline does not depend on the
                    scheduling parts.
backend/tests/      unittest. 211 tests.
```

## The team layer (added 2026-08-31)

`data/models/` holds registered models — teammates' endpoints, added via
`POST /api/models` or the Models tab. A registration stores connection
details only; `key_env` is the NAME of an environment variable and the
service layer rejects anything that does not look like one. Keys must never
reach disk or a response; there are tests on this too.

`POST /api/batch` runs one version across every callable registered model.
`GET /api/digest` (and `?format=text`) compiles verdicts by prompt, version
and model — compilation of the operator's verdicts, which is not a violation
of capture-only. `GET /api/runs?verdict=unrated` is the review queue.

A registered model's name is immutable after registration because runs are
labelled with it; deleting a registration must never touch its runs.

## Known gaps

The matrix shows only the newest run per cell when several exist behind it.
Batch runs are sequential — a slow model holds up the ones after it. The
2026-08-31 interface restructure (global tabs, Models, Digest, review filter)
was verified by production build and live API checks but not yet eyeballed in
a browser. Any of these is a reasonable thing to pick up; none is a bug.
