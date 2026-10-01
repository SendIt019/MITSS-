# Task: run metrics (time to first token and estimated FLOPs)

Requested by Jake on 2026-10-01. Today a run records prompt and completion
tokens, tokens per second, elapsed time, finish reason and the thinking text.
This task adds timing and compute metrics to every new run, and shows them in
the transcript, the run panel and the matrix summary.

## Branch

Start a new branch `feat/run-metrics` from `feat/mermaid-review` (447dbbd).
Commit locally only. Jake pushes.

## What to record (new keys in a run's `usage`)

All timings are measured on the generation request alone, after the preflight
probe, the same as the existing `tokens_per_second`.

| Key | Meaning |
|---|---|
| `time_to_first_token_ms` | Request sent → first streamed delta carrying any text (answer or thinking). For a local model this is mostly prompt processing. |
| `time_to_first_answer_ms` | Request sent → first delta carrying answer text. Differs from the line above only when the model thinks first. Null if the answer never started. |
| `prompt_tokens_per_second` | `prompt_tokens / time_to_first_token`. |
| `decode_tokens_per_second` | `(completion_tokens - 1) / (last delta time - first delta time)`. The generation speed with prompt processing taken out. |
| `active_params` | Parameters used per token, read from the model folder's `config.json`. For a mixture-of-experts model, count the shared weights plus `num_experts_per_tok` experts, not every expert. |
| `flops_estimate` | Estimated floating-point operations for the whole request (see below). |
| `flops_method` | A short string naming the formula, so a later change of method is visible in old runs. |

Keep `tokens_per_second` exactly as it is. The runner's time estimate and old
runs depend on its current meaning.

## FLOPs formula

Use the standard forward-pass estimate (Kaplan et al., 2020, "Scaling Laws
for Neural Language Models"). For each token processed:

    2 × active_params  +  2 × n_layers × context_length × attention_width

- `attention_width` is `num_attention_heads × head_dim`.
- `context_length` is how many tokens that token attends to. For prompt
  tokens it grows from 1 to `prompt_tokens`. For generated tokens it
  continues up to `prompt_tokens + completion_tokens`. Sum it in closed form;
  don't loop token by token.
- For layers with a sliding window (Gemma), cap the context at the window
  size, using `layer_types` / `sliding_window` from `config.json`.
- 4-bit quantisation does not change the count. Note that in `flops_method`.
- The total covers prompt and completion tokens together. Thinking tokens are
  completion tokens, so they count too.

Read `config.json` (or its `text_config`) with the standard library only.
Before writing code, read the three real configs in `~/Desktop/models/`
(llama-3.1-8b, gemma-4-26b-a4b, qwen3.8-27b). Your `active_params` must land
within 5 % of each model's published size: about 8.0 B for Llama, about 3.8 B
active for Gemma 4 26B-A4B, and Qwen's published figure (dense or active,
whichever its config shows). Put those checks in the step report.

If `config.json` is missing a field you need, or the architecture is one you
don't recognise, record `active_params` and `flops_estimate` as null, with
the reason in `flops_method`. Never guess a number.

## Where it shows

1. **`run.json`**: in `usage`, as above. Old runs are not rewritten. Runs are
   frozen, so they simply have no new keys.
2. **Transcript line** (`backend/pipeline/transcript.py` `format_usage`):
   add `first token 4.2 s`, `decode 7.6 tok/s` and `~1.3 PFLOPs`. Use
   readable units (GFLOPs, TFLOPs, PFLOPs) and the `~` mark, so it reads as
   an estimate.
3. **Run panel** (`frontend/src/settings.js`, the usage line that already
   shows tok/s): add the same three items. This is the only front-end change.
4. **Matrix `summary.txt`**: per model and in total, add the following,
   skipping runs that lack the new keys:
   - median time to first token
   - median decode tokens per second
   - total tokens in and out
   - total estimated FLOPs

   A model with no new-style runs shows `n/a`.

## Rules (the existing ones still apply)

- `backend/pipeline/` and `backend/mitss/` use the standard library only, on
  Python 3.9.
- Tests use the stub server. Extend the streaming stub so it can pause before
  the first delta and between deltas, then test the timings within a
  tolerance. Test the FLOPs formula with a small fake `config.json`, including
  a mixture-of-experts case and a sliding-window case. Test that null is
  recorded when a field is missing.
- No live models, no sudo, never read `backend/.env`. Registrations are not
  edited.
- Add a `DECISIONS.md` entry (append-only, timestamped) for the formula and
  for keeping `tokens_per_second` unchanged.
- Run every gate: unittest, compileall, pytest via uvx, ruff on py39,
  unittest on Python 3.9, and the frontend build.
- Then run the Codex review loop in `docs/runner/REVIEW_LOOP.md`, at most 3
  rounds. Save the reviews as `docs/runner/reviews/metrics-round-M.md`.
- If something fails: stop, explain, propose a plan, and wait for Jake.

## Step report

Include:
- files changed
- gates, with test counts
- the 3 real `active_params` figures next to the published sizes
- the decisions you logged
- the Codex verdict

Also give the live check for Jake to run himself: one Llama cell, then the
`summary.txt` lines to look at.
