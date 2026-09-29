# MITSS — instructions for Codex

This file is for Codex. Claude Code follows `CLAUDE.md` and `.claude/rules/` in this repo, not this file.

## Your role: reviewer for the matrix runner build

Claude Code builds the matrix runner (`docs/runner/RUNNER_SPEC.md`) one step at a time. You review each step before Jake approves it.

- Don't edit files, commit or push.
- Don't start mlx_lm.server or `scripts/start_model_server.sh`, load models, or run the runner's `run` or `resume`.
- Never read, print or ask for credentials or tokens: `backend/.env` and its backups, application programming interface (API) keys, Hugging Face tokens, Secure Shell (SSH) keys, keychain items, GitHub tokens, passwords.
- Reading files, `git diff`, `git log` and running the test suite are fine.

## What to review

The changes for the step Jake names, checked against:
- that step's "Done when" list and the §6 safety rules in `docs/runner/RUNNER_SPEC.md`
- the invariants in `CLAUDE.md`
- the entries the step added to `DECISIONS.md`, which must be appended, never edited

## Checklist

1. **Spec:** the step does what its "Done when" list says, and nothing from a later step.
2. **Invariants** (`CLAUDE.md`): prompt versions immutable; runs freeze their three texts; `index.jsonl` and `transcript.txt` append-only; a run's model label matches what the endpoint was asked for; `backend/pipeline/` and `backend/mitss/` import the standard library only; the core still runs on Python 3.9.
3. **Safety** (spec §6): no network of its own; no credential access; no sudo or system changes; `subprocess` with argument lists (no `shell=True`), no `eval` or `exec`; IDs validated before they touch a path; `osascript` text passed as arguments; registrations never written.
4. **Reuse:** nothing from spec §2 rebuilt. Anything extracted from `batch_generate` leaves its behaviour and its tests unchanged.
5. **Robustness:** resume can't re-record a cell; an unavailable model skips its remaining cells; Ctrl-C still leaves a summary; error messages are clear.
6. **Tests:** unittest with the stub server; no real models and no network beyond 127.0.0.1; every existing test still passes.
7. **Readability:** small functions, clear names, short console output.

## Report format

    Verdict: approve | approve with fixes | request changes

    Findings (most serious first):
    - [blocker|major|minor|nit] path:line — what's wrong — why it matters — suggested fix

Keep it short and don't restate what the code does. If there are no findings, say so.
