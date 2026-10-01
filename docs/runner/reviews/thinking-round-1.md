# Thinking fix — round 1

Codex, run by Claude Code on 2026-10-01 with:

```
codex exec --sandbox read-only "Read AGENTS.md. Review the thinking-fix task: the changes on branch fix/thinking-runs since commit e49c45f. Check them against docs/runner/THINKING_FIX_TASK.md, §6 in docs/runner/RUNNER_SPEC.md, and the invariants in CLAUDE.md. Don't edit anything. Give me your verdict and findings in the AGENTS.md format." < /dev/null
```

(The first attempt left stdin open and waited on it until killed after 30 minutes; it produced no review. This is the rerun.)

Reviewed at `f4def77`. Its final message, verbatim:

---

Verdict: request changes

Findings (most serious first):

- [major] scripts/patch_mlx_lm.sh:47 — `--undo` restores the backup before checking the installed version or current file. After an mlx-lm upgrade, it can overwrite the new `cache.py` with the old 0.31.3 file. Validate the version and expected patched contents before restoring; test undo after an upgrade.
- [minor] .claude/settings.json:26 — The required patch-script ask rules are missing, as DECISIONS.md acknowledges. Add rules covering invocation with and without arguments before marking task A complete.

Verification: frontend tests passed (17/17). Backend discovery attempted 430 tests but encountered 314 errors because the read-only sandbox cannot create temporary directories; the backend gate remains unverified.
