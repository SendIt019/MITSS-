# Task: make thinking-on runs finish (Qwen crash, Gemma runaway and loops)

Requested by Jake on 2026-10-01. Build this after the run-metrics task
(`docs/runner/METRICS_TASK.md`) is approved. Both tasks change the streaming
reader in `backend/mitss/llm.py`, so branch `fix/thinking-runs` from
`feat/run-metrics`. Commit locally only.

## What happened (matrices from 2026-10-01)

- **qwen3.8-27b-think, 2 runs.** Both failed after about 33 minutes. The
  server log (`backend/matrices/overnight-logs/test-20261001-104452/server-qwen3.8-27b.log`)
  shows `RuntimeError: [metal::malloc] Resource limit (499000) exceeded`.
  The server's generation thread died, but the HTTP side stayed up, so the
  runner sat until its 300 s idle timeout.
- **gemma-4-26b-a4b-think, 2 runs.** Both hit max_tokens 32768.
  - Run 20261001-095923 spent the whole budget thinking (about 100k
    characters) and wrote no answer.
  - Run 20261001-110130 wrote Section 1, then repeated `N/A,` inside a
    `NONE AVAILABLE` row until the limit.

## Research (verify each point on Jake's install before relying on it)

1. **The Qwen crash is a known mlx-lm bug, not a memory shortage.** 499000
   is a cap on the *number* of live Metal buffers, not bytes.
   mlx-lm issue #1332 traces it, for hybrid linear-attention models such as
   qwen3_5 and qwen3_next, to `ArraysCache.advance()` in
   `mlx_lm/models/cache.py`: `self.left_padding -= N` builds an unevaluated
   graph node on every decode step. It crashes Qwen3.8-27B at about 10,560
   tokens, and it happens only in the batch/server path, not in plain
   `mlx_lm.generate`. The fix reported there, calling
   `mx.eval(self.left_padding)` inside `advance()`, allowed 14,000+ token
   completions and was faster.
   https://github.com/ml-explore/mlx-lm/issues/1332
2. **No released fix.** Jake runs mlx-lm 0.31.3, which is the latest on
   PyPI. Issue #1672 confirms the crash on 0.31.3 with Qwen 3.6 and 3.8, and
   notes that the server goes silent instead of failing.
   https://github.com/ml-explore/mlx-lm/issues/1672
   The "fail fast when the worker thread dies" fix (#1505, PR #1513) is on
   main, not in a release.
   https://github.com/ml-explore/mlx-lm/issues/1505
3. **Gemma 4's repetition collapse in long structured output is a known
   model-level problem.** Google's issue #622 reports that repetition
   penalty, temperature, top_p, top_k and min_p did *not* stop it. What
   helped was removing fields the model has to make up.
   https://github.com/google-deepmind/gemma/issues/622
   A penalty is worth a measured try (mlx_lm.server accepts
   `repetition_penalty`, `presence_penalty` and `frequency_penalty`), but it
   is not the fix.
4. **mlx_lm.server has no thinking budget.** A runaway thought has to be
   stopped on the MITSS side.

## Fixes

### A. Patch script for the Qwen leak (Jake runs it; you don't)

- First, read only:
  - `~/models-env/lib/python3.14/site-packages/mlx_lm/models/cache.py`, to
    confirm `ArraysCache.advance()` matches the issue
  - `~/Desktop/models/qwen3.8-27b/config.json`, to confirm the model type
    uses `ArraysCache`

  If either doesn't match, stop and report. Don't patch blind.
- Write `scripts/patch_mlx_lm.sh` (bash, `set -euo pipefail`). It should:
  - find the mlx_lm install in `~/models-env`
  - check that the version is 0.31.3, and refuse anything else
  - back up `cache.py` to `cache.py.orig-mitss`
  - add `mx.eval(self.left_padding)` (and `lengths`, if `advance()` changes
    it) at the end of `advance()`
  - be safe to run twice
  - support `--undo`, which restores the backup
  - print exactly what it changed
- Add the script to the ask list in `.claude/settings.json`. Log in
  `DECISIONS.md` that this patches a third-party package in Jake's
  environment, and that an mlx-lm upgrade replaces it.

### B. Crash watchdog in `scripts/start_model_server.sh`

Read the server's stderr. When a line contains
`RuntimeError: [metal::malloc]` (or any traceback from the generation
thread), print one clear line and stop the server. The runner then sees a
dropped connection at once and records a failure, instead of waiting through
the idle timeout. Pass stdout and stderr through unchanged, and keep the
script's current arguments.

### C. Loop detection in the streaming reader (`backend/mitss/llm.py`)

- While streaming, watch the last few hundred characters of the answer and
  of the thinking separately.
- If a short unit (up to about 60 characters) repeats back to back past a
  threshold (default 40 repeats, settable as `loop_repeats` in a
  registration's settings, with 0 meaning off), stop reading and close the
  connection.
- Keep everything received so far, and record
  `finish_reason: "repetition"` with the repeated unit in `usage`.
- Count it as truncated in the matrix summary.
- Standard library only. Test with the stub server, including a legitimate
  repeated pattern such as 24 CSV rows, which must **not** trigger it.

### D. Thinking budget

- New optional registration setting `thinking_budget`: a token count, with 0
  or missing meaning none.
- Count reasoning deltas as they stream; mlx_lm.server sends about one token
  per delta, so note that the count is approximate.
- If the count passes the budget before any answer text has arrived, stop
  and record `finish_reason: "thinking_budget"`, keeping the thinking text.
  This fails fast rather than salvaging an answer; say so in the decision
  entry.
- Show both new finish reasons in the transcript, the run panel and
  `summary.txt`.

### D2. Settings plumbing

- `thinking_budget` and `loop_repeats` are MITSS-side settings: accept them in
  `normalize_settings` (non-negative integers), and never send them to the
  model server in the request body.
- `repetition_penalty` (number at least 1.0) and `frequency_penalty` (number)
  are not in `SETTING_NAMES` today. mlx_lm.server accepts both, so add them and
  pass them through like `presence_penalty`.

### E. Registration payloads for Jake (print them; don't write them)

Registrations are data, so give Jake the exact payloads to apply:

- `qwen3-8-27b-think`:
  - `max_tokens: 9000`, under the roughly 10.5k crash point, until the patch
    is verified
  - `thinking_budget: 5000`
  - `loop_repeats: 40`
- `gemma-4-26b-a4b-think`:
  - `max_tokens: 16384`
  - `thinking_budget: 8000`
  - `loop_repeats: 40`
- One experimental variant, `gemma-4-26b-a4b-think-rp`: the same settings
  plus `repetition_penalty: 1.1`, to test whether a penalty helps at all.

### F. Prompt fix for the loop trigger

Both Gemma failures involved the `NONE AVAILABLE` row. Write the text of a
new version 3 of `lite-comms-plan-v4` to `docs/runner/lite-comms-plan-v4-v3.txt`
for Jake to upload. Prompt versions are immutable, so don't create it in the
store. Make only these changes to version 2:

1. Replace "every data field in double quotes" with "no commas inside any
   value; use semicolons". Fix this everywhere the quote rule appears.
2. Show one complete `NONE AVAILABLE` row with all 25 fields written out.
3. Add bands to the equipment reference: Link 16 is 960-1215 MHz and line of
   sight only; SRW and ANW2 are UHF/L-band, not 30-88 MHz.
4. Tell the model to stagger windows and periodicity between links.

Diff it against version 2 in the step report.

## Rules

The existing rules apply:
- standard library only in `backend/pipeline/` and `backend/mitss/`, on
  Python 3.9
- stub-server tests only
- no live models, no sudo, never read `backend/.env`
- don't edit registrations
- `DECISIONS.md` entries are appended with timestamps
- every gate must pass
- run the Codex review loop in `docs/runner/REVIEW_LOOP.md`, at most 3
  rounds, saving reviews as `docs/runner/reviews/thinking-round-M.md`
- if anything fails: stop, explain, propose a plan, and wait for Jake

## Live validation is already scripted

`backend/matrices/validate/thinking-validate.sh` (written by Jake's planning
chat) runs the live checks and raises the limits after they pass. Don't run
it, and don't edit it; if your names differ from this task's (`thinking_budget`,
`loop_repeats`, `repetition_penalty`, `finish_reason` values `repetition` and
`thinking_budget`, `scripts/patch_mlx_lm.sh`), say so in the step report.

## Step report

Include:
- files changed
- gates, with test counts
- what you found in `cache.py` and the Qwen config
- the registration payloads
- the prompt v3 diff
- the Codex verdict

Then give Jake the live checks, in order:

1. Run `scripts/patch_mlx_lm.sh`.
2. Start a fresh qwen3.8-27b server and run one qwen-think cell with
   `max_tokens` temporarily at 16384. It passes if it gets past 11k tokens
   without the malloc error. If it passes, Jake raises `max_tokens`.
3. Run gemma-think and gemma-think-rp on prompt v3, input 01.
