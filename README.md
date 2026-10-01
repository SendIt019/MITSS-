# MITSS

A prompt engineering pipeline. You write a prompt, run it through your own
model, and bring the output back. MITSS keeps the exact prompt, the exact
output, which model produced it, and what you concluded on reading it — and
lets you compare any two results.

```
frontend/   React interface (Vite)
backend/    Python: FastAPI over a dependency-free pipeline core
```

The harness does not judge output. You do, by reading it. What the harness
provides is that your reading is not thrown away, and that "which version of
the prompt, on which model, produced this" always has an answer.

## Quick start

```bash
./dev.sh
```

Backend on `http://127.0.0.1:8000`, interface on `http://127.0.0.1:5173`,
frontend dependencies installed on first run.

Running local models with mlx_lm.server? `scripts/start_model_server.sh
[model] [port]` starts it with the standard flags (gemma-3-12b on 8080 by
default) and refuses a port that is already taken.

Manually:

```bash
cd backend  && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000
cd frontend && npm install && npm run dev
```

Requirements: Python 3.9 or newer, Node 18 or newer.

## The cycle

Create a prompt, or drop a `.txt` file in. Put `{input}` where the material
goes. Pick an input set, copy the **rendered** prompt, run it through your
model, and paste the output back with the model's name on it. Read the output
and set a verdict — accurate, partly right, or inaccurate — plus any note.

Then edit the prompt. Saving does not overwrite: it creates the next version,
so every output stays tied to the exact text that produced it. Run the new
version on the same input, record that output too, and the matrix fills in.

## Prompts and inputs are separate

A prompt version is the **wording** you are tuning. An input set is the
**material** it gets applied to. Keeping them apart is what makes the version
history mean anything: v1 against v2 on the same input is a fair test of
wording, and one input run across every version answers whether a rewrite
actually helped.

```text
PROMPT v2                     INPUT "Acme incident"
  Extract entities from         Acme Corp confirmed the
  the passage.                  outage on 4 March 2026...
  PASSAGE:
  {input}
                    ↓ rendered ↓
  Extract entities from the passage.
  PASSAGE:
  Acme Corp confirmed the outage on 4 March 2026...
```

The copy button copies the rendered prompt, never the template. Inputs are
editable, unlike prompt versions — safe because each run freezes the template,
the input and the rendered result separately, so editing an input later cannot
rewrite what a past run was given.

If a prompt has no `{input}` placeholder, a chosen input is appended at the end
with a warning rather than silently dropped.

## Versions are immutable

This is the one rule the pipeline enforces. A prompt version is written once
and never edited. That is what makes a run from three weeks ago still mean
something: `prompt.txt` inside the run folder is the text that was actually
sent, frozen at the moment of recording, independent of anything you have
changed since.

Saving a prompt that is byte-identical to the current version is refused,
because a version that changed nothing only adds noise to every later
comparison.

## Comparing

The **matrix** puts prompt versions down the side and models across the top.
Each cell shows the worst verdict recorded for that pairing — a model that got
it wrong once is the thing worth noticing, even if a retry passed. Click a cell
to read the output behind it.

Filter it to a single input set to compare wording fairly. Left on "all
inputs" it still renders, which is useful for coverage, but it says plainly
that it is mixing inputs and is therefore not a like-for-like comparison. It
also lists version and model pairings that have never been run.

**Compare** puts any two outputs side by side with a word-level difference
highlight. Word-level matters: a line diff on model output reports a reworded
sentence as one line deleted and one line added, which tells you nothing. This
shows you that `14:15 UTC` became `not stated` and left the other nine lines
alone.

Removed text is struck through and added text is underlined, so the comparison
survives greyscale printing and colour-vision deficiency rather than depending
on the red and green.

## Diagrams in outputs

When a recorded output contains fenced `` ```mermaid `` blocks, the run panel
(Outputs tab, open a run) draws each one as a diagram under the output text,
in the order they appear. The output text itself is unchanged above them.

- A block that does not parse shows Mermaid's parse error and the raw code in
  its place. A block with no closing fence (an answer cut off at the token
  cap) says so.
- **Save PNG** under each diagram downloads it at twice its on-screen size,
  on a white background, named `<run id>-diagram-<n>.png`.
- Outputs with no Mermaid block look exactly as they did before; the Mermaid
  library is not even downloaded for them.
- Model output is untrusted. Mermaid runs with `securityLevel: "strict"`, a
  `%%{init}%%` directive in the output cannot change that, and the drawing is
  shown as an image, so nothing in it can run script. The page's content
  security policy (`frontend/index.html`) stops a diagram from fetching
  anything while Mermaid lays it out: a block that names an image URL fails
  with a message instead.
- Mermaid is an npm dependency bundled by Vite, so this works offline.

## Plugging in your model

The default provider is `manual`: MITSS gives you a prompt and takes an output,
and never calls anything. To have the backend call your model directly, copy
`backend/.env.example` to `backend/.env`:

```bash
MITSS_LLM_PROVIDER=http
MITSS_LLM_URL=http://127.0.0.1:8080/v1/chat/completions
MITSS_LLM_FORMAT=openai        # or "raw" for {"prompt": ...}
MITSS_LLM_MODEL=my-custom-model
MITSS_LLM_MODELS="team-7b, team-70b"   # optional: offer a choice in the interface
```

With `MITSS_LLM_MODELS` set, the Prompt tab shows a **Run on** picker and a
**Run** button — pick a model, click once, and the output is fetched and
recorded against that model. See `SANDBOX.md` for the full variable table and a
reproducible end-to-end check.

The `openai` shape works with llama.cpp, vLLM, Ollama and LM Studio unchanged.
For anything else, subclass `LLMProvider` in `backend/mitss/llm.py` and call
`register_provider("myname", MyProvider)`.

## Team models

The environment variables above configure one provider for the whole backend.
The **Models** tab is the many-models version: each teammate registers their
model once — a name, who owns it, the endpoint URL, the body shape — and it
becomes a **Registered model** choice on the Prompt tab, a column in the
matrix, and part of every batch run. Registering over HTTP works too:

```bash
curl -X POST http://127.0.0.1:8000/api/models \
  -H 'Content-Type: application/json' \
  -d '{"name": "team-7b", "owner": "alex",
       "url": "http://10.0.0.5:8080/v1/chat/completions",
       "format": "openai", "model": "team-7b-q4",
       "key_env": "TEAM_7B_KEY"}'
```

A registration is connection details, never credentials: `key_env` is the
*name* of an environment variable set on the machine running the backend, and
a value that looks like a key rather than a variable name is refused outright.
The API reports whether that variable is set, never what it holds.

An entry with no URL is **paste-only**: the backend cannot call it, but runs
pasted back under its name stay consistently labelled, so it still gets a
matrix column. Names are fixed after registration for the same reason —
recorded runs carry them.

**Run all** appears next to the registered-model picker once two or more
callable models are registered: one click sends the current version and input
to every one of them, records each reply as an ordinary run, and reports
which models failed without stopping the rest.

## The digest

The **Digest** tab — and `GET /api/digest` — rolls everything recorded up by
prompt, version and model: verdict tallies, per-model records across every
prompt, and the review queue of outputs nobody has read yet. It compiles your
verdicts; it never judges an output itself.

`GET /api/digest?format=text` is the same digest as a plain-text page,
downloadable from the tab, readable without the tool, and compact enough to
paste into a model as context. `GET /api/runs?verdict=unrated` is the review
queue on its own.

Credentials are read from the environment at call time, sent once in the
request header, and never logged, returned by the API, or written into stored
data. `GET /api/llm` reports whether a key is present, never its value.

## Matrix runner

One terminal command runs a whole comparison without anyone watching: every
chosen version of a prompt × every chosen input × every chosen registered
model, with an optional repeat count. Each answer is recorded as an ordinary
run, so it shows up in Outputs, the matrix, the transcript and the review
queue. A Mac notification with a sound tells you when the matrix finishes or
stops. The runner records; you judge. Nothing is scored.

It calls local models only (127.0.0.1, localhost, ::1) and always goes through
`scripts/run_matrix.sh`, which loads `backend/.env` the way `dev.sh` does and
gives each model load up to 300 seconds (`MITSS_MATRIX_PREFLIGHT_TIMEOUT`
overrides that). Run everything from the repo root.

### Before a run

Start the model server with `scripts/start_model_server.sh`, then check:

```bash
scripts/run_matrix.sh check
```

`check` calls no model. It confirms each lineup registration (or the ones you
name with `--models`), its model folder, weights and quantization, the GPU
memory limit, free disk space and that the server is listening. It also sends
a test notification. Abridged output:

```
GPU memory limit: 20.0 GiB (iogpu.wired_limit_mb)

qwen3.8-27b (id qwen3-8-27b)
  settings: temperature=0.2  max_tokens=3072  timeout=120s  enable_thinking=false
  folder /Users/jakel/Desktop/models/qwen3.8-27b
  weights 14.1 GiB, 4-bit
...
Server http://127.0.0.1:8080/v1/chat/completions: listening
Test notification sent

check: 0 problem(s), 0 warning(s)
```

If a model's weights plus 2 GiB are over the memory limit, `check` prints the
`sudo sysctl iogpu.wired_limit_mb=...` command for you to run yourself. It
never runs sudo, and the setting resets when the Mac restarts.

### A matrix file

JSON, conventionally in `backend/matrices/`:

```json
{"name": "resume-2x2", "prompt_id": "lite-comms-plan-from-m-salute",
 "versions": [2, 3], "inputs": ["01-scout-sam-battalion", "02-cav-division-120h"],
 "models": ["llama-3-1-8b"], "repeats": 1}
```

`models` holds registration ids (the Models tab shows them; `qwen3.8-27b` is
registered as `qwen3-8-27b`). List the fastest model first so problems show up
early. Each model runs all of its cells before the next one loads. `repeats`
is 1 to 10 and defaults to 1. Every id must exist, and a quarantined,
paste-only or non-local model is refused before anything runs.

### Commands

```bash
scripts/run_matrix.sh plan   MATRIX.json [--models ID[,ID...]]
scripts/run_matrix.sh run    MATRIX.json [--models ID[,ID...]] [--yes]
scripts/run_matrix.sh resume MATRIX_RUN_ID [--allow-changed-inputs]
scripts/run_matrix.sh status [MATRIX_RUN_ID]
```

`plan` checks the file and shows the cells and an estimate, with no model
calls:

```
$ scripts/run_matrix.sh plan backend/matrices/resume-2x2.json
Matrix resume-2x2: prompt lite-comms-plan-from-m-salute, versions 2, 3, inputs 01-scout-sam-battalion, 02-cav-division-120h, repeats 1

  1. llama-3.1-8b  4 cells  ~3m  (23.9 tok/s from 10 recent run(s), loading not included)
     id llama-3-1-8b - temperature=0.2  max_tokens=3072  timeout=120s  enable_thinking=false

Total: 4 cells, ~3m
```

`run` prints the same plan, checks the server, and asks before starting
(`--yes` skips the question). `--models` runs only some of the file's models.
It prints one line per cell, then the summary. `resume` finishes an
interrupted or partly failed matrix: it never records a finished cell again,
and it refuses if an input it still needs was edited since the plan, unless
you pass `--allow-changed-inputs`. `status` lists matrix runs, or shows one:

```
$ scripts/run_matrix.sh status
20260929-144947-smoke-3-models  3/3 recorded  0 failed  0 skipped  (finished)
20260929-145953-resume-2x2  4/4 recorded  0 failed  0 skipped  (finished)
20260929-150813-resume-2x2  4/4 recorded  0 failed  0 skipped  (finished)
```

Exit codes: 0 every cell recorded, 1 finished with failed or skipped cells,
2 stopped early (or not started), 3 the matrix or preflight was refused.

**Ctrl-C.** Press it once and the cell in progress finishes and records, then
the matrix stops, writes its summary and notifies you. Press it twice and the
matrix stops at once. The request in flight is abandoned, and the model server
may keep generating until it finishes. Either way, `resume` picks up from
there, using the id the summary prints.

### Reading the summary: check the truncated count

Every `run` and `resume` ends with a summary, per model and in total:

```
  llama-3.1-8b: recorded 4/4 · failed 0 · skipped 0 · truncated 1 · 5m03s
```

Check the **truncated** count before comparing anything. A truncated answer
stopped at the model's `max_tokens` (`finish_reason: length`) instead of
ending on its own. It means one of two things, and only reading the output
tells you which:

- **The cap is too low.** Answers of a normal length are cut off mid-way. Raise
  `max_tokens` on the registrations and run again. At 1024, all three answers
  in the first smoke test and two of four llama-3.1-8b answers were
  truncated. At 3072, three of four llama-3.1-8b answers finished on their
  own.
- **The model is looping.** It repeats itself and would run into any cap, so a
  higher cap only makes the loop longer. That is a verdict on the model, not
  a runner fault. The one answer still truncated at 3072 was llama-3.1-8b
  looping on `02-cav-division-120h`.

Two MITSS-side guards also end a reply early, and count as truncated too:

- **Loop detection** (`finish_reason: repetition`). A unit of up to 60
  characters repeated back to back `loop_repeats` times (default 40, over at
  least 200 characters) in the answer or in the thinking stops the stream.
  The run keeps everything received and records `repetition_unit`,
  `repetition_count` and `repetition_in` (answer or thinking) in `usage`. A
  registration's `loop_repeats: 0` turns it off.
- **Thinking budget** (`finish_reason: thinking_budget`). With a
  registration's `thinking_budget` set, a model that streams more than that
  many reasoning deltas (about one token each) before any answer text is
  stopped, keeping its thinking. It fails fast; it does not try to salvage an
  answer.

Neither setting is sent to the model server. The summary names any reason
other than the cap, e.g. `truncated 2 (length 1, repetition 1)`, and the
console marks each truncated cell `TRUNCATED`, or `TRUNCATED (repetition)`.
A finished notification includes the count.

If mlx_lm.server's generation thread crashes (as Qwen 3.8 27B did with
`RuntimeError: [metal::malloc] Resource limit (499000) exceeded`),
`scripts/start_model_server.sh` stops the server at once, so the cell fails
straight away instead of waiting out the idle timeout.
`scripts/patch_mlx_lm.sh` patches mlx-lm 0.31.3 for that crash (see
DECISIONS.md, 2026-10-01).

### Where things go

Each matrix run gets its own folder, `backend/data/matrices/<matrix-run-id>/`:
`manifest.json` (every cell, fixed before the first call), `results.jsonl`
(each cell `started`, then `recorded`, `failed` or `skipped`) and
`summary.txt`. All three are append-only. The runs themselves are ordinary
runs in `data/runs/`.

A copy goes to `~/Desktop/AI Outputs/MITSS Runs/<matrix-run-id>/`: the same
three files, plus `runs/<run-id>/` for every recorded run, copied as each cell
finishes. A copy that fails is noted in the summary and never stops the
matrix.

### Timing and compute metrics

Every run generated through a model endpoint from 2026-10-01 carries,
in `usage`: `time_to_first_token_ms` (request sent to the first text,
thinking included - mostly prompt processing on a local model),
`time_to_first_answer_ms`, `prompt_tokens_per_second`,
`decode_tokens_per_second` (completion tokens after the first, over the time
between the first and last text), `active_params`, `flops_estimate` and
`flops_method`. `tokens_per_second` keeps its old meaning. The transcript
and the run panel show `first token 4.2 s`, `decode 7.6 tok/s` and
`~1.3 PFLOPs`; each model's line in `summary.txt` is followed by the median
first-token time and decode speed, total tokens in and out, and total
estimated FLOPs, or `metrics: n/a` when none of its runs has them.

The FLOPs figure is an estimate from the model folder's `config.json`
(Kaplan et al., 2020): `2 × active_params + 2 × layers × context ×
attention_width` per token, prompt and completion together. It is only
computed for architectures whose count was checked against the published
size (Llama, Mistral, Qwen 2, Qwen 3, Qwen 3 MoE, Qwen 3.5, Gemma 4).
Anything else, a remote endpoint, or a config missing a field is recorded
as null, with the reason in `flops_method`. Older runs are not rewritten.

### Known limits

- Runs are sequential, through one mlx_lm.server. A slow model holds up the
  ones after it.
- The matrix view shows only the newest run per cell, so review repeats from
  Outputs or the review queue.
- The estimate needs earlier runs of the model with a token rate, and it
  leaves out model loading.
- The memory check is advisory: weights plus 2 GiB against the limit. It
  cannot see what else is using memory.
- A second Ctrl-C abandons the request in flight, but the model server may
  keep generating until that answer is done.
- A first Ctrl-C pressed during the last cell is reported as **finished**
  (exit 0, or 1 with failed cells): every cell ran, so there is nothing left
  to resume.
- After an interruption, `resume` looks for a run recorded for the
  interrupted cell (same prompt, version, model and input, created at or
  after the cell started, to the second) and adopts it rather than asking
  the model again. A run of that same cell that you record by hand while the
  matrix is interrupted could be adopted in its place.
- A run adopted that way may be missing its `index.jsonl` event, if the
  interruption landed between the run folder and the event being written.
  The summary says so, and the event is not added afterwards.
- The copies outside the repo are taken when each run is recorded. Verdicts
  and notes set later in the interface change the run in `data/runs/`, not
  the copy.
- If copying `summary.txt` itself fails, that failure shows in `status` and
  in the next summary, not in the summary that failed to copy.

## What is on disk

Everything is plain files — readable and greppable without this application.

```
backend/data/
  prompts/<prompt-id>/
    prompt.json       name, tags, version index
    v1.txt, v2.txt    the exact text of each revision
  inputs/<input-id>/
    input.json        name, note, timestamps
    input.txt         the material
  models/<model-id>/
    model.json        a registered model: details, never credentials
  matrices/<matrix-run-id>/
    manifest.json     a matrix run's cells, fixed before the first call
    results.jsonl     append-only: each cell started, then its result
    summary.txt       append-only: one block per run or resume
  runs/<run-id>/
    run.json          model, input, verdict, notes, timestamps
    template.txt      the prompt version used
    input.txt         the input used
    prompt.txt        what those rendered into — exactly what was sent
    output.txt        the output exactly as returned
  transcript.txt      rolling plain-text log of every run, readable
  index.jsonl         append-only event log
```

`transcript.txt` is the file to open when you want to read the history rather
than click through it. Each run appends a block with the model, the version,
the input, the full rendered prompt, the full output and the verdict. It is
append-only: a verdict set after the fact appears as its own line further down
rather than being edited into the block above, so it reads as a log, not a
table. The interface links to it in the top right, and `GET /api/transcript`
serves it.

`index.jsonl` records every prompt created, version added, output recorded,
verdict set and run deleted. It is never rewritten, so deleting a run does not
erase the fact that it happened.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | liveness |
| `GET` | `/api/llm` | how the model harness is configured |
| `GET` | `/api/prompts` | list prompts |
| `POST` | `/api/prompts` | create a prompt |
| `GET` | `/api/prompts/{id}` | prompt with version history |
| `POST` | `/api/prompts/{id}/versions` | save a new version |
| `POST` | `/api/uploads` | upload a `.txt` as a prompt or a new version |
| `GET` | `/api/inputs` | list input sets |
| `POST` | `/api/inputs` | create an input set |
| `PATCH` | `/api/inputs/{id}` | edit an input set |
| `GET` | `/api/prompts/{id}/preview` | the rendered prompt for a version and input |
| `GET` | `/api/models` | the team's registered models |
| `POST` | `/api/models` | register a model (details, never credentials) |
| `PATCH` | `/api/models/{id}` | edit a registration (the name is fixed) |
| `DELETE` | `/api/models/{id}` | remove a registration; its runs remain |
| `POST` | `/api/runs` | record an output |
| `GET` | `/api/runs?verdict=unrated` | the review queue |
| `PATCH` | `/api/runs/{id}` | set your verdict and notes |
| `POST` | `/api/generate` | call the configured or a registered model |
| `POST` | `/api/batch` | one version across every registered model |
| `GET` | `/api/digest` | everything rolled up (`?format=text` for the page) |
| `GET` | `/api/prompts/{id}/matrix` | versions against models, optionally per input |
| `GET` | `/api/compare?a=&b=` | word-level diff of two outputs |
| `GET` | `/api/activity` | the append-only event log |
| `GET` | `/api/transcript` | the rolling plain-text transcript (`?download=true`) |

Interactive documentation at `http://127.0.0.1:8000/docs`.

## The scheduling example

`backend/mitss/` is a fully worked domain kept as an example: it parses a
plain-text plan, generates a prompt stating every rule an answer will be
checked against, and can validate a returned answer against those rules. Seed
it into the prompt library with:

```bash
cd backend && python -m examples.seed_scheduling
```

The pipeline itself knows nothing about scheduling. See
`backend/examples/README.md`.

## Tests

```bash
cd backend && python -m unittest discover tests
cd frontend && npm test && npm run build
```

The backend suite covers: the pipeline core (storage, immutable versioning, input
sets, prompt rendering, verdicts, diffing, the matrix, the transcript, the
model registry, the digest), the HTTP surface, the model harness and batch
runs — exercised against a real local server, including that an API key never
reaches an error message or disk — the matrix runner (`backend/run_matrix.py`,
including interruption and resume, against a stub server), and the scheduling
example's own suite.

`npm test` covers how Mermaid blocks are found in an output
(`frontend/tests/mermaidBlocks.test.js`, Node's built-in runner).
`npm run check:browser` builds the interface and drives the installed Chrome
through valid, invalid, mixed, unclosed and hostile Mermaid outputs and one
with none, then saves a PNG. It starts its own backend on a temporary data
root, so it never writes to `backend/data/`, and fails if any image named in
model output reaches a server.

## Design notes

The pipeline core under `backend/pipeline/` has no third-party dependencies and
no network access. FastAPI and uvicorn are needed only by `backend/app/`.

See `DECISIONS.md` for the full decision log, including what was reversed and
why.
