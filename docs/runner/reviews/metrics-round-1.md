# Run metrics — round 1

Codex, run by Claude Code on 2026-10-01 with:

```
codex exec --sandbox read-only "Read AGENTS.md. Review the run-metrics task: the changes on branch feat/run-metrics since commit 447dbbd. Check them against docs/runner/METRICS_TASK.md, §6 in docs/runner/RUNNER_SPEC.md, and the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format."
```

Reviewed at `76d9d35`. Its final message, verbatim:

---

Verdict: request changes

Findings (most serious first):
- [major] backend/app/service.py:445 — `_with_metrics` skips enrichment when server usage is absent. This discards measured stream timings and available parameter counts, violating the requirement to add metrics to every new generated run; the matrix summary also excludes these runs. Always add the seven keys, using null and a reason where counts are unavailable, and add a stub test without server usage.

Validation: 15 frontend tests and 11 formula/timing tests passed. Real configs yield 8.030 B, 3.822 B active, and 26.895 B—each within 5% of the task’s stated sizes. Full backend testing was blocked by the read-only sandbox’s temporary-directory restriction; remaining build gates were not run.
