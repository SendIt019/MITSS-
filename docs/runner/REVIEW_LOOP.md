# Matrix runner — review loop

From Step 1's fixes on (Jake, 2026-09-29), Claude Code runs the Codex review
itself instead of waiting for Jake to paste one.

## Running a review

After a step or a round of fixes is committed, run Codex read-only from the
repo root:

```bash
codex exec --sandbox read-only "Read AGENTS.md. Review Step N of the matrix runner build: the changes on the runner branch since commit ABC1234. Check them against Step N's \"Done when\" list and §6 in docs/runner/RUNNER_SPEC.md, and against the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```

The prompt is the Codex prompt in the spec's appendix, with the step number
and the commit the step started from filled in. `--sandbox read-only` was
checked against `codex exec --help` (codex-cli 0.159.0). If a flag is ever
rejected, use the equivalent read-only option from `--help`. Codex is never
run in a mode that can write files.

## Rules

- Save every review verbatim to `docs/runner/reviews/step-N-round-M.md` and
  commit it with that round's fixes.
- Fix every blocker and major. Fix minors too, unless one conflicts with the
  spec or a logged decision.
- Disagreements are not argued out with Codex. Stop and ask Jake, with
  Codex's finding and Claude Code's reasoning side by side.
- At most 3 review rounds per step. If Codex is not at "approve" or "approve
  with fixes" after round 3, stop and ask Jake.
- Stop when Codex approves, with a short report: rounds taken, what changed,
  anything declined or disputed, and any commands Jake needs to run.
- Never start the next step without Jake. Never run anything that needs a live
  model; that stays with Jake.

Round 1 of Step 1 was the review Jake pasted with his decisions, before this
loop existed; it is recorded as `reviews/step-1-round-1.md`.
