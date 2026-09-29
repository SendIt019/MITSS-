# Matrix runner build — rules for Claude Code

The repo's `CLAUDE.md` still applies in full. These rules add to it while the matrix runner in `docs/runner/RUNNER_SPEC.md` is being built.

- **One step at a time** (spec §8). A step ends with the gates passing, a `DECISIONS.md` entry in that file's own format, a local commit on the runner branch, and a step report. Don't start the next step until Jake says so.
- **Codex reviews each step.** When Jake pastes a review, fix the blockers and majors, rerun the gates, log any decisions, and stop again.
- **Reuse, don't rebuild.** Spec §2 lists what the service and core already do. Record runs only through the service.
- **Registrations are data.** Never create, edit or delete one. Print the exact payload or command for Jake.
- **No live models.** Don't start mlx_lm.server or `scripts/start_model_server.sh`, load a model, or run the runner's `run` or `resume`. Tests use the stub server. When a check needs a live model, print the command and wait for Jake's output.
- **No sudo or system changes**, including `iogpu.wired_limit_mb`. Print the command instead.
- **Never read `backend/.env` or its backups.** The credentials rule in `CLAUDE.md` covers them.
- **Don't push.** Jake does that.
- **No front-end changes** in v1.
- **When something fails:** stop, explain in plain language what happened and why, propose a plan, wait for Jake's OK, then carry it out. Don't retry blindly or work around a failing gate.

**Step report**, kept short: files changed · gates (pass or fail, with test counts) · decisions logged · commands for Jake · anything that conflicts with the spec.
