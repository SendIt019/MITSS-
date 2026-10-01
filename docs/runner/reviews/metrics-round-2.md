# Run metrics — round 2

Codex, run by Claude Code on 2026-10-01 with:

```
codex exec --sandbox read-only "Read AGENTS.md. Review the run-metrics task: the changes on branch feat/run-metrics since commit 447dbbd. Check them against docs/runner/METRICS_TASK.md, §6 in docs/runner/RUNNER_SPEC.md, and the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```

Reviewed at `47cdcfc`. Its final message, verbatim:

---

Verdict: approve with fixes

Findings (most serious first):

- [minor] backend/run_matrix.py:947 — Missing token counts are summed as zero, so a run without reported usage shows `0 in / 0 out tokens`; mixed runs silently show partial totals. Display `n/a` when counts are absent and label partial totals with the number of missing runs. Add summary tests for both cases.

Validation: 15 frontend tests and 11 formula/timing tests passed. Real configs yield 8.030 B, 3.822 B active, and 26.895 B—within 5% of the task’s stated sizes. Full backend testing was blocked by sandbox temporary-directory restrictions; remaining gates were not run.
