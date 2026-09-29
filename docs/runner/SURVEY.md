# Matrix runner — Step 0 survey

Read from the repo on 2026-09-29 at `a60b66e` on `fix/streaming-and-model-paths`.
Every claim below points at the file it was checked in. Paths are from the repo
root. Nothing here was run against a live model.

## 1. Spec §2, confirmed or corrected

| §2 claim | Status | Where |
|---|---|---|
| Model registry in `backend/data/models/` with connection details, per-model settings incl. `chat_template_kwargs`, and quarantine | Confirmed | `backend/pipeline/store.py:339-453`, settings validated by `normalize_settings` in `backend/mitss/llm.py:237`, quarantine in `backend/app/service.py:256-281` |
| `batch_generate(prompt_id, version, input_id, model_ids)` runs one version × one input across models, sequentially, recording each success as an ordinary run | Confirmed | `backend/app/service.py:474-559`; each model goes through `generate_run` (line 525) |
| "Connection errors and idle timeouts get one retry" in `batch_generate` | **Corrected: the retry is lower down.** It is in `HttpProvider.generate` (`backend/mitss/llm.py:560-571`) and in the preflight (`llm.py:588-595`), so it applies to every call, including a plain `generate_run`. The runner gets it for free. | `backend/mitss/llm.py:46-49` |
| 404 or missing model folder makes the model unavailable for the rest of the batch | Confirmed, with a detail: unavailability is keyed by `(url, model or name)`, the thing actually served, not by registration ID. Two registrations of the same folder share the outcome. | `backend/app/service.py:503, 518-527, 544-547`; the flag is set in `_fetch_and_record` (`service.py:364-383`) |
| Quarantined models are skipped | Confirmed in `batch_generate` (`service.py:509-516`). `generate_run` refuses them with 409 (`service.py:449-454`). | |
| Results carry `finish_reason` and `truncated` | Confirmed | `service.py:531-536` |
| One log line per model | Close: two per model, "asking..." then the outcome | `service.py:528, 539, 551` |
| One-token preflight probe; stuck server returns 503 | Confirmed. Probe timeout defaults to 90 s (`MITSS_LLM_PREFLIGHT_TIMEOUT`); the model load happens inside the probe. | `llm.py:575-599`, `llm.py:27-29`, `service.py:370-373` |
| Streamed requests with an idle-gap timeout | Confirmed; idle default 120 s (`MITSS_LLM_TIMEOUT`), overridable per registration with `settings.timeout` | `llm.py:24-28, 548-554` |
| Typed errors | Confirmed | `llm.py:79-115`, mapped to status codes in `service.py:360-383` |
| Redaction of echoed credentials | Confirmed | `llm.py:297`, used at `llm.py:655-656, 721` |
| Frozen `template.txt`, `input.txt`, `prompt.txt` | Confirmed | `store.py:524-534` |
| Settings snapshot in `run.json` | Confirmed; see §3 below | `service.py:384-392`, `models.py:182-186, 218` |
| `usage` and tokens per second | Confirmed; rate only when the server reported a real token count | `service.py:395-414` |
| `input_sha256` | Confirmed | `store.py:41-43, 507`, `models.py:196, 223` |
| `reasoning.txt` when the model reasoned | Confirmed | `store.py:531-533`, parsed at `llm.py:739-758, 816-824` |
| `index.jsonl` and `transcript.txt` append-only | Confirmed | `store.py:618-623` (`"a"` mode), `pipeline/transcript.py:124-142` |
| One mlx_lm.server; requests name the model by folder path | Confirmed. All twelve current registrations use `http://127.0.0.1:8080/v1/chat/completions` and an absolute folder path in `model`. For a local URL a bare name is resolved to `MITSS_MODELS_DIR/<name>` and the folder's `config.json` is checked before any request. | `llm.py:160-181, 529-535`; `backend/data/models/*/model.json` (read, not changed) |
| `scripts/start_model_server.sh` runs under `caffeinate` | Confirmed (`caffeinate -i`, port 8080 by default, which matches the registrations) | `scripts/start_model_server.sh:43-46` |
| `backend/export_runs.py` (untracked) exports CSV and JSONL, writes nothing in `data/` | Confirmed; default output `backend/exports/`. It reads `backend/data` directly and ignores `MITSS_ROOT`. | `backend/export_runs.py:105-107, 118` |
| Tests: unittest, 127.0.0.1 stub server, throwaway model folders | Confirmed | stub `_Handler` in `backend/tests/test_team.py:33-55`; `backend/tests/model_folders.py` |

Also verified on this Mac (read-only): `sysctl -n iogpu.wired_limit_mb hw.memsize`
returns `0` and `25769803776` (24 GB, default limit). All three lineup folders
exist with a `config.json`:

| Folder | Size on disk | `*.safetensors` | Top-level `bits` |
|---|---|---|---|
| `llama-3.1-8b` | 4.2 GB | 1 | 4 |
| `qwen3.8-27b` | 14 GB | 3 | 4 |
| `gemma-4-26b-a4b` | 14 GB | 3 | 4 (its router projections are 8-bit per layer) |

`llama-3-1-8b` is registered with no settings block, so today it runs at the
repo defaults (temperature 0, `MITSS_MAX_TOKENS`).

## 2. How a run is recorded, and what the runner should call

`generate_run(prompt_id, version, input_id=..., model_id=..., root=...)`
(`service.py:429-471`):

1. resolves the version (`_resolve_version`, `service.py:339-347`) and the input;
2. renders the prompt (`pipeline/render.py`);
3. loads the registration, refuses it if quarantined or paste-only;
4. builds an `HttpProvider` from the registration (`_provider_for`, `service.py:417-426`);
5. `_fetch_and_record` (`service.py:350-392`) calls the model, maps errors to
   `ServiceError` (with `.status`, `.message` and `.unavailable`), then calls
   `Store.create_run` (`store.py:470-522`), which writes the run folder, appends
   to the transcript and appends a `run_recorded` event to `index.jsonl`;
6. returns the run as a dict (`id`, `usage` incl. `finish_reason`, and so on).

`batch_generate` adds only bookkeeping around that call: quarantine skip,
the `(url, model)` unavailability map, the `finish_reason == "length"` check,
and log lines.

**Recommendation: the runner calls `service.generate_run` once per cell and does
not extract anything from `batch_generate`.** `generate_run` already is the
per-model call path. The runner's own loop needs slightly different
bookkeeping (it refuses quarantined models at plan time and records skips per
cell, not per model), so sharing the ~30 lines of loop body would force a
signature change on `batch_generate` for little gain. `batch_generate` and its
tests stay untouched.

## 3. What the manifest can refer to

- **`input_sha256`**: `text_sha256(text)` = SHA-256 of the UTF-8 input text
  (`store.py:41-43`), stored in each run's `run.json` at record time
  (`store.py:507`). The manifest should compute plan-time hashes with the same
  function (`pipeline.store.text_sha256`) on `service.get_input(id)["text"]`,
  so a manifest hash compares directly with a run's `input_sha256`.
- **Settings snapshot**: `run.json` → `settings` is exactly what went in the
  request body (temperature, max_tokens, any registration settings such as
  `chat_template_kwargs`) plus the client idle `timeout`; never the key
  (`llm.py:537-545`, `service.py:384-392`). The manifest does not need its own
  copy; it can point at `run_id`.
- **Reasoning**: `reasoning.txt` in the run folder, only when non-empty;
  `run.json` has `reasoning_characters` (`models.py:221`).
- **Usage**: `run.json` → `usage` has the server's token counts,
  `finish_reason` and `tokens_per_second` when available. `results.jsonl` can
  copy `finish_reason` and `truncated` from the returned run dict, the same
  way `batch_generate` does.

## 4. Where the manifest lives

**Recommendation: `<data root>/data/matrices/<matrix_run_id>/`**, where the data
root is whatever `service.store()` resolves (`MITSS_ROOT`, else `backend/`;
`service.py:43-44`). Use `service.store().data_dir`, not a hard-coded path, so
the runner and the web app always agree.

The store ignores it:
- `Store` knows exactly four subfolders (`prompts`, `runs`, `inputs`,
  `models`; `store.py:76-89`) and every listing goes through `_list_ids` on one
  of them (`store.py:94-110`).
- The only other files it touches in `data/` are `index.jsonl` (`store.py:615`)
  and `transcript.txt` (`pipeline/transcript.py:27`).
- Nothing in `backend/app`, `backend/pipeline` or `backend/mitss/llm.py` lists
  `data/` itself (checked with grep for `listdir`, `scandir`, `walk`, `glob`).
- `export_runs.py` reads only `data/runs/`.
- `.gitignore` already covers it (`backend/data/`).

`matrix_run_id` should use the store's own alphabet, `[a-z0-9][a-z0-9-]{0,127}`
(`store.py:61`), e.g. `20260929-141500-baseline-v1`, so it is safe as a
directory name and consistent with run IDs.

## 5. Gate commands

From CI (`.github/workflows/ci.yml`) and the 2026-09-22 gates entry in
`DECISIONS.md`. There is no `.venv` in the repo right now; the system
`python3` is 3.14 and has FastAPI installed, so API tests run rather than skip.

```bash
cd backend
python3 -m unittest discover tests                                   # all tests
python3 -m compileall -q .                                           # syntax
uvx --with fastapi --with python-multipart --with httpx2 pytest -q tests
uvx ruff check <changed .py files>                                   # changed files only
uv run --no-project --python 3.9 python -m unittest discover tests   # bare 3.9, API tests skip
```

Baseline at `a60b66e`, run for this survey: unittest 284 OK; compileall clean;
pytest 284 passed; 3.9 bare suite 284 OK with 71 skipped (matches CI); ruff
0.16.9 available, nothing to check (no `.py` changed in Step 0). There is no
ruff configuration file, so ruff runs with its defaults.

The frontend build (`npm ci && npm run build` in `frontend/`) is in CI but v1
changes no front-end files.

## 6. Which branch to start from

State: `fix/streaming-and-model-paths` is pushed and level with its remote,
3 commits ahead of `main` (`36ba962`, `cadfb23`, `a60b66e`); `main` equals
`origin/main` at `a0bbe82`. No open pull requests.

Those three commits are exactly what §2 relies on: per-model settings,
preflight, reasoning capture, `input_sha256`, streaming, quarantine and 404
handling. Branching from `main` would lose all of it.

**Recommendation: a new branch `feat/matrix-runner` from `a60b66e`** (the current
HEAD). The fix branch can still be merged to `main` on its own; the runner
branch then rebases or merges cleanly on top. If you would rather merge the fix
branch into `main` first, branching from the new `main` is equally good.
Committing runner work onto `fix/streaming-and-model-paths` itself would mix
two reviews into one branch.

Untracked files, to decide with the branch:
- `docs/runner/`, `AGENTS.md`, `.claude/settings.json`,
  `.claude/rules/runner.md`: build and review instructions; suggest committing
  them in the Step 0 commit on the runner branch.
- `backend/export_runs.py`: your call (spec §9).
- `backend/exports/`: exported copies of run text (`runs.csv`, `runs.jsonl`,
  `mermaid/`, `darkhorse/`). Suggest not committing it and adding
  `backend/exports/` to `.gitignore`, the same reasoning as `backend/data/`.

## 7. Module name and command

**`backend/run_matrix.py`**, run from `backend/` like `export_runs.py`:

```bash
cd backend
python3 run_matrix.py check
python3 run_matrix.py plan   matrices/baseline.json
python3 run_matrix.py run    matrices/baseline.json [--models ID[,ID...]] [--yes]
python3 run_matrix.py resume MATRIX_RUN_ID [--allow-changed-inputs]
python3 run_matrix.py status [MATRIX_RUN_ID]
```

It imports `from app import service` (`backend/app/__init__.py` is empty and
`service.py` imports only the standard library, `mitss.llm` and `pipeline`), so
it stays standard-library only and outside the FastAPI boundary. Tests go in
`backend/tests/test_run_matrix.py`, which the CI 3.9 job picks up automatically.
Matrix files live where Jake keeps them; `backend/matrices/` is the suggested
default (not under `data/`, since they are inputs he writes, not store data).

**`.claude/settings.json` ask rules**: the six existing rules already match
`python3|python run_matrix.py run|resume *`. They do not match other spellings
of the same command, such as `python3 backend/run_matrix.py run …` or
`uv run … run_matrix.py run …`. Two broader rules would cover those:

```json
"Bash(*run_matrix.py run *)",
"Bash(*run_matrix.py resume *)"
```

I tried to add them and the edit to `.claude/settings.json` was refused by
the permission system, so they are **not applied**. Add them yourself if you
want them.

## 8. Where the spec and the code disagree, or need a decision

1. **Environment from `backend/.env` is not loaded by the runner.** `dev.sh`
   sources `backend/.env` into the shell (`dev.sh:7-12`); nothing in Python
   reads it. A runner started from a plain terminal sees none of those
   values, and it must not read `.env` itself. What that can change:
   `MITSS_ROOT` (the runner would write runs into a different store than the
   web app reads), `MITSS_LLM_TIMEOUT`, `MITSS_LLM_PREFLIGHT_TIMEOUT`,
   `MITSS_MAX_TOKENS`, `MITSS_MODELS_DIR`. Proposal: set `max_tokens` and
   `timeout` on each lineup registration (Step 1 already prints that block),
   and have `check` print the data root and the effective values of these
   non-secret variables. Jake can confirm whether `.env` sets `MITSS_ROOT`
   without printing values: `grep -c '^MITSS_ROOT=' backend/.env`.
2. **Preflight timeout vs. a cold load of a 14 GB model.** The model load
   happens inside the one-token probe, which has 90 s by default
   (`llm.py:575-582`), and a registration cannot change it (only `timeout`
   is a setting, `llm.py:203`). If loading qwen3.8-27b or gemma-4-26b-a4b
   takes longer, the cell fails as "stuck" (503), not "unavailable", so the
   next cell reloads and may fail the same way. Proposal: `check` reports the
   value, and the run command Jake uses exports a longer one, e.g.
   `MITSS_LLM_PREFLIGHT_TIMEOUT=300`. Measure it in Step 3.
3. **A Ctrl-C window that could duplicate a cell.** The run is recorded
   inside `generate_run` (`service.py:386-392`). A Ctrl-C that lands after
   `create_run` returns but before the runner writes the `results.jsonl`
   line leaves a recorded run that resume does not know about, and resume
   would record the cell again. A Ctrl-C during `create_run` can also leave a
   run folder without its `index.jsonl` event (`store.py:497-521`). The window
   is small but real, and spec §5.4 says resume "reads only the manifest and
   `results.jsonl`". Options for Step 2: (a) accept the window and document
   it; (b) let resume also read `index.jsonl` `run_recorded` events since the
   matrix started and adopt a matching run instead of re-recording it
   (read-only, keeps append-only). I recommend (b); it changes the §5.4
   wording, so it is your call.
4. **Unavailability is per served model, not per registration** (see §1).
   The runner should key skips the same way, `(url, model)`, so two
   registrations of one folder cannot both burn a load.
5. **IDs vs. names.** Matrix files use registration IDs (`qwen3-8-27b`,
   `gemma-4-26b-a4b`, from `slugify`, `pipeline/models.py:297-300`); runs,
   console lines and the transcript use the registration name
   (`qwen3.8-27b`). The spec's examples already follow this; noting it so
   the plan output shows both.
6. **Quantization bits for Gemma 4** are mixed (4-bit body, 8-bit router
   projections). `check` should report the top-level `bits` and say "mixed"
   when per-layer overrides exist, rather than parse every layer.
7. **`llama-3-1-8b` has no settings today.** "Same settings for all three"
   means Step 1's printed payload must include a `PATCH` for it too, not just
   the two new registrations.
8. **Step 0 commit.** §8 says each step ends with a local commit on the runner
   branch. That branch does not exist until you choose it, so Step 0 is not
   committed yet.

## Resolved by Jake after the survey (2026-09-29)

- Branch: `feat/matrix-runner`, created from `a60b66e`.
- Ask rules: Jake updated `.claude/settings.json` himself, with
  `Bash(*run_matrix* run *)` and `Bash(*run_matrix* resume *)`. These also
  cover `scripts/run_matrix.sh`.
- `backend/.env` sets `MITSS_LLM_PROVIDER`, `MITSS_LLM_FORMAT`,
  `MITSS_LLM_URL`, `MITSS_LLM_MODELS`, `MITSS_LLM_MODEL` and
  `MITSS_LLM_TIMEOUT`, and not `MITSS_ROOT`. Item 8.1's store mismatch
  therefore does not happen today; the timeout difference does.
- Item 8.1 and 8.2: a wrapper, `scripts/run_matrix.sh`, loads `.env` like
  `dev.sh` and sets the preflight timeout (spec §5.1).
- Item 8.3: option (b), plus `started` lines and a two-stage Ctrl-C (spec §5.4).
- Items 8.4 to 8.7: agreed as proposed.
- Untracked files: `docs/runner/`, `AGENTS.md` and `.claude/` committed in
  Step 0; `backend/exports/` gitignored; `backend/export_runs.py` left untracked.
