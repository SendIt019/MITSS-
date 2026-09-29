## 2026-09-29 10:47 CDT — Matrix runner: goal

Jake wants one command that runs prompts across the models he sets up, tells
him when it is done, and leaves the outputs in the harness for his review.
Evaluation stays manual. Planned in a claude.ai chat (Harness project).

## 2026-09-29 11:02 CDT — Runner v1 is local only

The model Jake is tuning prompts for is one of the local open-source models.
No cloud calls and no API keys in v1. Cloud chats stay manual for now; API
access may come later.

## 2026-09-29 11:02 CDT — Claude Code builds, Codex reviews

Claude Code builds the runner in gated steps, Codex reviews each step
read-only, and Jake approves before the next step starts. A second model
checks the work, and the two agents never edit the same files.

## 2026-09-29 11:22 CDT — Lineup: newest Llama, Qwen and Gemma that fit in 24 GB

llama-3.1-8b, qwen3.8-27b and gemma-4-26b-a4b, all 4-bit MLX builds in
`~/Desktop/models/`.

- Qwen3.8-27B (released August 2026) is the newest Qwen that fits.
- Gemma 4 26B-A4B is the newest Gemma that fits. The dense 31B is over the
  memory budget.
- Nothing newer than Llama 3.1 8B fits: Llama 4 Scout and Maverick are 109B
  and about 400B parameters. Meta's newer open model, Muse Glimmer 30B,
  needs 18-20 GB at 4-bit and an add-on loader for mlx-lm.
- The Qwen and Gemma builds are about 15-16 GB, over the default GPU memory
  limit on a 24 GB Mac (about 16 GB). Runs that include them need
  `sudo sysctl iogpu.wired_limit_mb=20480` first, run by Jake. It resets on
  restart.

Jake confirmed all three were downloaded at 11:47.

## 2026-09-29 11:47 CDT — Runner v1 behaviour

- Started from the terminal. No front-end changes.
- Each model finishes all of its cells before the next one loads.
- Same generation settings for every lineup model, thinking off, one repeat
  by default.
- Every result is an ordinary MITSS run (Outputs, matrix, transcript, review
  queue), plus a copy outside the repo.
- A Mac notification with sound when a matrix finishes or stops. Counts
  only, never output text.
- Interrupted runs resume without re-recording finished cells.
- Tests use the stub server. Agents never load real models.
- No scheduling. No automated scoring.

## 2026-09-29 11:47 CDT — Deferred from runner v1

Cloud providers and API keys, manual slots for cloud chats, scenario
generation from input sets, prompt-version generation, blind review, and a
Run button in the interface.

## 2026-09-29 12:13 CDT — Runner builds on the existing batch path

The first draft of the runner spec assumed a separate tool with its own model
config, server management and storage. The repo already has most of that:
the registry (with per-model settings and quarantine), `batch_generate`, the
preflight probe, streamed requests with idle timeouts, reasoning capture,
and one mlx_lm.server that swaps models by folder path. The spec was
revised. The runner adds only a matrix, model-major ordering, resume, a
memory check, notifications, a copy outside the repo and a summary, and it
records every result through the service layer. "Same settings for every
model" is done on the registrations, not with a new override.

Open: the copy folder (default `~/Desktop/AI Outputs/MITSS Runs`), the
settings values (suggested temperature 0.2, max_tokens 1024, thinking off),
which branch the runner work starts from, and whether `export_runs.py` gets
committed.
