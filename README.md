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

## Plugging in your model

The default provider is `manual`: MITSS gives you a prompt and takes an output,
and never calls anything. To have the backend call your model directly, copy
`backend/.env.example` to `backend/.env`:

```bash
MITSS_LLM_PROVIDER=http
MITSS_LLM_URL=http://127.0.0.1:8080/v1/chat/completions
MITSS_LLM_FORMAT=openai        # or "raw" for {"prompt": ...}
MITSS_LLM_MODEL=my-custom-model
```

The `openai` shape works with llama.cpp, vLLM, Ollama and LM Studio unchanged.
For anything else, subclass `LLMProvider` in `backend/mitss/llm.py` and call
`register_provider("myname", MyProvider)`.

Credentials are read from the environment at call time, sent once in the
request header, and never logged, returned by the API, or written into stored
data. `GET /api/llm` reports whether a key is present, never its value.

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
| `POST` | `/api/runs` | record an output |
| `PATCH` | `/api/runs/{id}` | set your verdict and notes |
| `POST` | `/api/generate` | call the configured model directly |
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
cd frontend && npm run build
```

179 backend tests: the pipeline core (storage, immutable versioning, input
sets, prompt rendering, verdicts, diffing, the matrix, the transcript), the HTTP surface, the
model harness — exercised against a real local server, including that an API
key never reaches an error message — and the scheduling example's own suite.

## Design notes

The pipeline core under `backend/pipeline/` has no third-party dependencies and
no network access. FastAPI and uvicorn are needed only by `backend/app/`.

See `DECISIONS.md` for the full decision log, including what was reversed and
why.
