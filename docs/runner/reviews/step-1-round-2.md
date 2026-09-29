# Step 1 — round 2

Codex, run by Claude Code on 2026-09-29 with:

```
codex exec --sandbox read-only "Read AGENTS.md. Review Step 1 of the matrix runner build: the changes on the runner branch since commit 49822f8. Check them against Step 1's \"Done when\" list and §6 in docs/runner/RUNNER_SPEC.md, and against the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```

Reviewed at `bd54941`. Its final message, verbatim:

---

Verdict: approve with fixes

Findings (most serious first):

- [minor] backend/run_matrix.py:604 — `check` resolves an empty model field to the models directory, while the service falls back to the registration’s name. Valid registrations can fail the folder check. Use `entry["model"] or entry["name"]` and add a regression test.
- [minor] backend/run_matrix.py:523 — An invalid URL port passes plan validation but crashes `check` with an uncaught `ValueError`. Validate ports and report a clear problem with exit code 3; test malformed and out-of-range ports.

Verification: unittest discovery ran 329 tests, but 235 errored under the read-only sandbox, which prevents temporary-directory creation. Gates remain unverified here. Step 1 completion also requires Jake’s registration application and successful Mac `check`/`plan` runs; that evidence is absent from the reviewed changes.
