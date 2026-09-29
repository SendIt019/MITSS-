# MITSS Matrix Runner — Build Spec (v2)

Owner: Jake · Builder: Claude Code · Reviewer: Codex · Revised: 2026-09-29, after reading the repo; again after Step 0 (`docs/runner/SURVEY.md`) · Status: Steps 0–4 done, on branch `feat/matrix-runner`; usage and known limits in the README's "Matrix runner" section

## 1. Purpose

One terminal command that runs a whole comparison without anyone watching: every chosen version of a prompt × every chosen input × every chosen model, with an optional repeat count. Every result is recorded as an ordinary MITSS run, so it shows up in Outputs, the matrix, the transcript and the review queue. When the comparison finishes or stops, Jake gets a notification.

Capture-only stands. The runner records; Jake judges. Nothing is scored.

## 2. What already exists (reuse it, don't rebuild it)

Read from the repo on 2026-09-29, on branch `fix/streaming-and-model-paths`. Step 0 checked every line; `docs/runner/SURVEY.md` §1 has the file references.

- **Model registry** (`backend/data/models/`): connection details, per-model generation settings (including `chat_template_kwargs` for turning thinking off), and quarantine. Registrations are working data.
- **`service.batch_generate(prompt_id, version, input_id, model_ids)`**: one version × one input across several models, sequential. Each success is recorded as an ordinary run, through `generate_run`. An HTTP 404 or a missing model folder makes that model unavailable for the rest of the batch; the key is the served `(url, model)` pair, not the registration, so two registrations of one folder share the outcome. Quarantined models are skipped. Results carry `finish_reason` and `truncated`. Log lines per model ("asking..." and the outcome).
- **Per-call safeguards:** a one-token preflight probe (a stuck server returns 503 instead of hanging; the model load happens inside it, with a 90 s default limit from `MITSS_LLM_PREFLIGHT_TIMEOUT`), streamed requests with an idle-gap timeout, typed errors, and redaction of echoed credentials. Connection errors and idle timeouts get one retry. That retry lives in `HttpProvider.generate` and its preflight (`backend/mitss/llm.py`), not in `batch_generate`, so every call gets it, `generate_run` included.
- **Environment:** only `dev.sh` loads `backend/.env`; no Python code reads it. A process started from a plain terminal sees none of its values.
- **Run records:** frozen `template.txt`, `input.txt` and `prompt.txt`; a settings snapshot in `run.json`; `usage` and tokens per second; `input_sha256`; `reasoning.txt` when the model reasoned. `index.jsonl` and `transcript.txt` are append-only.
- **One mlx_lm.server for every local model.** Requests name the model by its folder path, and the server unloads the previous model and loads the new one. `scripts/start_model_server.sh` starts the server under `caffeinate`, so the Mac stays awake while it runs.
- **`backend/export_runs.py`** (untracked): exports recorded runs to CSV (comma-separated values) and JSONL (JSON Lines), writing nothing inside `data/`.
- **Tests:** unittest, with a 127.0.0.1 stub server and throwaway model folders (`tests/model_folders.py`).

## 3. What's new in v1

1. **Matrix.** Several versions × several inputs × chosen models × repeats in one command. `batch_generate` covers one version × one input.
2. **Model-major order.** Every cell for one model runs before the next model loads, so each model loads once. Loading takes about 30 s for the 8B model and longer for the 27B. Calling `batch_generate` once per cell would swap models on nearly every call.
3. **Resume.** An interrupted or partly failed matrix picks up where it stopped and never re-records a finished cell.
4. **Memory check** before starting: model sizes compared with the graphics processing unit (GPU) memory limit, with the command Jake runs to raise it.
5. **Notification with sound** when the matrix finishes or stops.
6. **A copy outside the repo** of the matrix's run folders.
7. **A plain-text summary.**

Already handled, so not built again: model swapping, retries, stuck-server detection, reasoning capture, settings snapshots, the transcript, the review queue, and keeping the Mac awake.

## 4. Decisions carried in

The log is in `docs/runner/DECISIONS-planning.md`; Step 0 appends it to `DECISIONS.md`.

- **Local only.** No cloud calls and no application programming interface (API) keys.
- **Lineup:** llama-3.1-8b (already registered as `llama-3-1-8b`), qwen3.8-27b and gemma-4-26b-a4b (not registered yet). All 4-bit MLX builds in `~/Desktop/models/`.
- **Same generation settings for all three, thinking off**, set on each registration. That's the repo's existing mechanism, so no new override is needed.
- **Roles:** Claude Code builds, Codex reviews each step, Jake approves.
- **Terminal command.** No front-end changes.
- **No scheduling. No automated scoring.**

## 5. Design

### 5.1 Where it lives

One standard-library module, `backend/run_matrix.py`, with its tests in `backend/tests/test_run_matrix.py`. It must run on Python 3.9, because continuous integration (CI) runs the core on 3.9. That rules out `tomllib`, so the matrix file is JSON (JavaScript Object Notation). Matrix files go in `backend/matrices/` by default.

Jake starts it through a wrapper, `scripts/run_matrix.sh`, which:

1. loads `backend/.env` the same way `dev.sh` does, when it exists;
2. sets `MITSS_LLM_PREFLIGHT_TIMEOUT` to `MITSS_MATRIX_PREFLIGHT_TIMEOUT` if that is set, and to 300 otherwise, so a cold load of a 14 GB model is not reported as a stuck server;
3. runs `backend/run_matrix.py` with the same arguments, from the caller's directory, so a relative matrix path means what it says.

`run_matrix.py` never opens `.env` itself. Run directly, it uses whatever environment it was given.

The runner calls `service.generate_run` in-process, once per cell; `service.py` imports only the standard library and the core. Nothing is extracted from `batch_generate`, which stays unchanged (SURVEY §2).

### 5.2 Matrix file

```json
{
  "name": "baseline-v1",
  "prompt_id": "PROMPT-ID",
  "versions": [3, 4],
  "inputs": ["INPUT-A", "INPUT-B"],
  "models": ["llama-3-1-8b", "gemma-4-26b-a4b", "qwen3-8-27b"],
  "repeats": 1
}
```

- The prompt and input IDs are placeholders; Step 1 uses real ones. `models` holds registration IDs, fastest model first so problems show up early.
- Every version, input and registration must exist. A quarantined or non-callable model is refused at plan time, not skipped silently.
- `repeats` is 1–10. The matrix view shows only the newest run per cell (a known gap in `CLAUDE.md`), so repeats are reviewed from Outputs or the review queue.
- `name` follows the repo's ID rules and is validated before it touches a path.

### 5.3 Commands

From the repo root:

```
scripts/run_matrix.sh check
scripts/run_matrix.sh plan   MATRIX.json
scripts/run_matrix.sh run    MATRIX.json [--models ID[,ID...]] [--yes]
scripts/run_matrix.sh resume MATRIX_RUN_ID [--allow-changed-inputs]
scripts/run_matrix.sh status [MATRIX_RUN_ID]
```

- `check` — the model server is listening at the lineup registrations' address; each lineup model's folder has a `config.json`; size and quantization bits (the top-level `bits`, and "mixed" when per-layer overrides exist, as in Gemma 4); the memory limit; free disk space; a test notification. It prints the data root and the effective values of `MITSS_ROOT`, `MITSS_LLM_TIMEOUT`, `MITSS_LLM_PREFLIGHT_TIMEOUT`, `MITSS_MAX_TOKENS` and `MITSS_MODELS_DIR`, and nothing else from the environment. No model calls.
- `plan` — validates a matrix file and prints the cells per model, the total, and an estimated time where recent runs give a tokens-per-second figure (median rate and output length of the model's last 10 such runs, output capped at its `max_tokens`, loading not included). Takes `--models` like `run`. No model calls.
- `check` checks the lineup (`llama-3-1-8b`, `qwen3-8-27b`, `gemma-4-26b-a4b`) unless `--models` names other registrations. Its exit code is 3 when it finds a problem (not registered, quarantined, paste-only, missing folder, server not listening); memory, disk and notification findings are warnings.
- `run` — prints the plan and waits for `y` unless `--yes` is given. `--models` narrows the run to some models: the quick mode for iterating on one.
- `resume` — finishes an interrupted or partly failed matrix.
- `status` — lists matrix runs, or one run's progress.

Exit codes: 0 every cell recorded · 1 finished with failed or skipped cells · 2 stopped early · 3 validation or preflight error.

Console output stays short: one line per cell (`[qwen3.8-27b 3/8] v4 × INPUT-B … recorded 41.2 s`), with `TRUNCATED` when the answer stopped at the token cap, then the summary.

### 5.4 Run flow

1. Validate the matrix file against the store: prompt, versions, inputs, registrations, quarantine.
2. Preflight. If the server isn't listening, stop and print the `scripts/start_model_server.sh` command. Run the memory check (§5.5) and the disk-space check.
3. Before any call, write the matrix manifest at `<data root>/data/matrices/<matrix_run_id>/manifest.json`, found through `service.store().data_dir` so it follows `MITSS_ROOT` like the rest of the store (SURVEY §4). `matrix_run_id` uses the store's ID alphabet, e.g. `20260929-141500-baseline-v1`. It lists every cell in order: grouped by model, then version, then input, then repeat. For each input it records the `input_sha256` at plan time.
4. For each model, run each of its cells through `service.generate_run`. Before each call, append a `started` line for the cell to `results.jsonl`, with the time. After the call, append its result line: the cell, and either its `run_id` or the failure reason, plus `finish_reason`, `truncated` and elapsed time. Then copy the recorded run folder to the copy location (§5.6). When the service marks a model unavailable (404 or missing folder), record the remaining cells for that served `(url, model)` as skipped with the reason and move to the next model.
5. Write `summary.txt`, send the notification, exit.

`results.jsonl` events, one JSON object per line, each with `at` (store local time to the second): `session` and `session_end` (outcome and reason) around each `run` or `resume`; `started`, then one of `recorded` (`run_id`, `finish_reason`, `truncated`, `elapsed_ms`, plus `adopted` and `index_event` when resume adopted it), `failed` (`error`, `status`) or `skipped` (`reason`) per cell; `copy_failed` when a copy outside the repo could not be made (noted in the summary, never fatal). A line cut short by a hard stop is skipped by the reader and closed off before the next append. A 503 from the service (a stuck model server) is a fatal stop, exit 2: every later cell would fail the same way. Declining the `run` prompt exits 2 with nothing written.

**Resume** reads the manifest and `results.jsonl`, and checks only the inputs and models of cells still to run (a recorded cell froze its texts): a changed input is refused unless `--allow-changed-inputs`, a deleted input or a model that is now unregistered, quarantined, paste-only or not on loopback is always refused. It runs every cell that has no recorded `run_id` and never re-records one that does. A cell with a `started` line but no result may have been recorded just before an interruption. For those cells only, resume looks for the run in the run folders, not in `index.jsonl`. The run folder is the record: `create_run` writes it before the transcript entry and the `index.jsonl` event, and `run.json` is the last file of the folder to be written (`backend/pipeline/store.py:497-534`). A second Ctrl-C can land between those writes, so an index-only check could miss a run that Outputs, the matrix and the review queue already show. Resume calls `service.list_runs` with the cell's prompt, version, model name and input, and takes the earliest run whose `created_at` is at or after the `started` time and whose `run_id` no result line has already claimed. The comparison is at whole-second precision in the same time zone: `created_at` is the store's local time to the second (`store.now()`), so the `started` time is written the same way, with the same function, and compared as a time, not as text. If it finds one, it appends a result line adopting that `run_id` instead of calling the model again. If that run has no `run_recorded` event in `index.jsonl`, the result line and the summary say so. Resume never writes the missing event or transcript entry itself. A folder without `run.json` is not a run to the store (`list_runs` skips it), so that cell is run again. All of this is read-only; nothing in `data/` is rewritten. Prompt versions are immutable, but inputs can be edited, so resume compares each input's current hash with the one in the manifest. If any changed, it refuses and lists them, unless `--allow-changed-inputs` is given.

**Ctrl-C.** The first Ctrl-C lets the current cell finish and record, then stops cleanly: it writes the summary, sends a "stopped" notification and exits 2. When that cell was the last one, nothing is left to stop, so the matrix is reported as finished (exit 0, or 1 with failed cells). A second Ctrl-C stops at once. The request in flight is abandoned (the server may keep generating until it finishes, as it does today), the summary is still written, and the cell is left with a `started` line for resume to reconcile.

### 5.5 Memory check

- Weights = the total size of the model folder's `*.safetensors` files.
- Limit = `iogpu.wired_limit_mb` when it's non-zero. Otherwise Metal's recommended working-set size, read through the `~/models-env` Python (Step 1 confirms the call). If neither can be read, two-thirds of RAM on Macs with 36 GB or less, three-quarters above. The call, confirmed in Step 1 with mlx 0.32.3: `mx.device_info()['max_recommended_working_set_size']`, falling back to `mx.metal.device_info()` on older mlx; the Python is `$MITSS_MODELS_ENV/bin/python`, default `~/models-env`, the same variable `scripts/start_model_server.sh` uses. On Jake's 24 GB Mac it reads 17.8 GiB.
- When weights plus 2 GB exceed the limit, warn and print `sudo sysctl iogpu.wired_limit_mb=20480` for Jake to run himself (or the next whole GiB above weights plus 2 GB, when that is more). It resets on restart. A limit that would leave macOS less than 4 GiB is never suggested; the model is reported as not fitting instead.
- Reading needs no sudo: `sysctl -n iogpu.wired_limit_mb hw.memsize`. The runner never runs sudo.

### 5.6 Copy outside the repo

Default location: `~/Desktop/AI Outputs/MITSS Runs/<matrix_run_id>/`. It holds the manifest, `results.jsonl`, `summary.txt`, and a copy of each recorded run folder, copied as each cell finishes. These are plain file copies; nothing in `data/` changes. `export_runs.py` can turn them into CSV later; whether to commit it is Jake's call.

### 5.7 Notifications

- **Finished:** title "MITSS matrix finished", body like "24 recorded · 0 failed · 2 truncated · 48m", sound "Glass".
- **Stopped:** title "MITSS matrix stopped", a one-line reason, and the resume command (`scripts/run_matrix.sh resume MATRIX_RUN_ID`), with sound "Basso" so it sounds different from a finished matrix.
- Counts only. Never prompt, input or output text.
- The text is passed to `osascript` as arguments, never built into a script string:
  `osascript -e 'on run argv' -e 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"' -e 'end run' TITLE BODY`

### 5.8 Summary and status

`summary.txt` is append-only like the other two files: each `run` or `resume` appends a block, and the copy folder gets the whole file. It gives per-model counts (recorded, failed, skipped, truncated), time per model, each failure with its reason, and where everything is. `status` reads the manifest and `results.jsonl` and prints progress.

## 6. Safety rules

These add to the invariants in `CLAUDE.md`.

1. No network of its own. Model calls go through the service, to loopback only: `plan` and `check` refuse a registration whose URL is not 127.0.0.1, localhost or ::1.
2. No credentials. Never read `backend/.env` or its backups.
3. No sudo and no system-setting changes; print the command for Jake instead.
4. `subprocess` with argument lists only (`osascript`, `sysctl`). No `shell=True`, `eval` or `exec`.
5. IDs are validated before they touch a path.
6. Nothing in `data/` is rewritten. Runs are recorded only through the service, and the manifest and `results.jsonl` are append-only.
7. Registrations are data. The runner never creates or edits them; changes are printed for Jake to apply.
8. Standard library only, and it runs on Python 3.9.
9. Environment: for the runner, `backend/.env` is loaded only by `scripts/run_matrix.sh`, the same way `dev.sh` loads it. `run_matrix.py` never opens it. `check` prints only the data root and the five variables named in §5.3, never any other environment value.
10. Reading never writes: `check` and `plan` read through a read-only store that creates nothing under `data/`, and read registrations as the store's own summary, never through `service.get_model`, so a key variable is never looked up, not even to see whether it is set.

## 7. Tests

unittest, matching the repo: the 127.0.0.1 stub server, throwaway model folders, and temporary data roots. Cover:

- plan validation: unknown IDs, a quarantined model, bad repeats, an unsafe name
- model-major ordering
- an unavailable model skipping its remaining cells
- resume without re-recording, and refusing when an input changed
- resume, for a cell with a `started` line and no result: adopting a run found in the run folders, including one with no `index.jsonl` event (reported, not repaired); not adopting a run another result line already claimed (repeats of the same cell); adopting a run recorded in the same second as the `started` line, and not one recorded the second before; and calling the model again when there is none, or only a folder without `run.json`
- the first Ctrl-C finishing the current cell, the second stopping at once, and fatal stops, all still writing the summary
- `check` printing only the named environment variables
- the copy folder's contents
- the memory math
- the `osascript` and `sysctl` commands built correctly, with nothing actually sent

Every existing test must still pass, including the Python 3.9 no-dependencies suite.

**Gates:** the repo's own, pinned in Step 0 (SURVEY §5). From `backend/`:

```bash
python3 -m unittest discover tests
python3 -m compileall -q .
uvx --with fastapi --with python-multipart --with httpx2 pytest -q tests
uvx ruff check --target-version py39 <changed .py files>   # must pass
uv run --no-project --python 3.9 python -m unittest discover tests
```

## 8. Build steps

Each step ends with the gates passing, a `DECISIONS.md` entry, a local commit on the runner branch, and a short step report. Jake has Codex review the step, then approves the next one. Anything that needs a live model is run by Jake, from commands the agent prints.

### Step 0 — Survey and setup (no source changes)

1. Append `docs/runner/DECISIONS-planning.md` to the bottom of `DECISIONS.md`, entries unchanged.
2. Write `docs/runner/SURVEY.md`, answering each item with file paths:
   1. Confirm or correct §2.
   2. How `generate_run` and `batch_generate` record a run, what the runner should call, and whether the per-model call path needs extracting.
   3. How `input_sha256`, the settings snapshot and reasoning are recorded, so the manifest can refer to them.
   4. Where the manifest should live, and confirmation that the store ignores folders it doesn't know.
   5. The exact gate commands.
   6. Which branch the runner work should start from. The current branch, `fix/streaming-and-model-paths`, is 3 commits ahead of `main`, and `export_runs.py` and `exports/` are untracked. Recommend; Jake decides.
   7. The final module name and command, with the `ask` rules in `.claude/settings.json` updated to match.
   8. Anything in this spec that conflicts with the code.

Done when: `SURVEY.md` answers all eight, the planning entries are appended, no source files changed, and Jake has chosen the branch.

### Step 1 — Registrations, check, plan

- Print, don't apply, the registration payloads for qwen3.8-27b and gemma-4-26b-a4b, and a `PATCH` for `llama-3-1-8b`, which has no settings block today. All three carry the same settings, including `max_tokens` and `timeout`. Confirm the thinking-off argument for Gemma 4 by reading its chat template (read-only).
- Build `scripts/run_matrix.sh` (§5.1), matrix-file loading and validation, `check` and `plan`.

Done when: tests and gates pass, and Jake has applied the registrations and run `check` and `plan` on his Mac with the expected output.

### Step 2 — Engine, resume, copy, notification, summary

Build `run`, `resume` and `status`, the manifest and `results.jsonl`, the copy, the notification and the summary.

Done when: tests cover every item in §7 and the gates pass.

### Step 3 — End to end, with Jake

With the server started from the script and the memory limit raised, Jake runs 1 version × 1 input × all three models. The outputs appear in Outputs, the matrix and the review queue, in the transcript, and in the copy folder, and the notification fires. Then Jake starts a 2 × 2 matrix, interrupts it with Ctrl-C, and `resume` finishes it without duplicates.

### Step 4 — Docs

A README section in which every documented command was actually run (the repo's "verify, don't assert" rule), the final `DECISIONS.md` entries, and a known-limits list. The list must say that a manual run of the same cell (same prompt, version, model and input), recorded while a matrix is interrupted, could be adopted by resume in place of the cell's own run. It must also say that a first Ctrl-C pressed during the last cell is reported as finished (exit 0, or 1 with failed cells), because every cell ran and there is nothing left to resume.

## 9. Open items

- Copy folder: `~/Desktop/AI Outputs/MITSS Runs` unless Jake names another.
- Settings for the three registrations. Suggested: temperature 0.2, max_tokens 1024, thinking off, plus an idle `timeout`. The repo's default for a blank registration is temperature 0.
- Settled at Step 0: the runner works on `feat/matrix-runner` from `a60b66e`; `export_runs.py` stays untracked for now (the runner doesn't need it); `backend/exports/` is gitignored.
- Later phases: cloud providers, manual slots for cloud chats, scenario generation, prompt-version generation, blind review.

## Appendix — prompts for each step

**Claude Code, Step 0.** Start Claude Code in `~/Desktop/MITSS`, then paste:

> Read docs/runner/RUNNER_SPEC.md and .claude/rules/runner.md. Do Step 0 only, with no source changes: append docs/runner/DECISIONS-planning.md to DECISIONS.md, write docs/runner/SURVEY.md, then stop and give me the step report.

**Codex, after each step**

> Read AGENTS.md. Review Step N of the matrix runner build: the changes on the runner branch since commit ABC1234. Check them against Step N's "Done when" list and §6 in docs/runner/RUNNER_SPEC.md, and against the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format.

**Claude Code, fixing review findings**

> Codex's review of Step N is below. Fix the blockers and majors, rerun the gates, log any decisions, then stop and report. Don't start Step N+1.
>
> [paste the review]

**Claude Code, next step**

> Step N is approved. Do Step N+1 only.
