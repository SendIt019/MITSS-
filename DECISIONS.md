# DECISIONS

Append-only. Newest entries go at the bottom. Never edit or delete an entry —
if a decision is reversed, add a new entry that says so and why.

Format: `## YYYY-MM-DD HH:MM TZ — short title`

---

## 2026-08-10 14:30 EDT — Repository seeded from GitHub

Cloned `SendIt019/MITSS-` to `~/Desktop/MITSS`. Repo was private; made public
briefly for the clone, then reverted. Git history and the `origin` remote are
intact, so `git pull` / `git push` work normally.

Starting contents were a single README reading "I/O for Box".

## 2026-08-10 14:35 EDT — Scope: harness, not solver

MITSS is an input/output harness around a language model, not a scheduling
algorithm. The harness never calls a model and never solves anything itself. It
stages inputs, builds a prompt packet, captures the reply, validates it, checks
it against hard constraints, renders it, diffs it against prior runs, and logs
everything.

Rationale: the model does the scheduling; the value here is that its output gets
checked rather than trusted. Keeping the solver out means the harness stays
useful regardless of which model answers.

## 2026-08-10 14:36 EDT — Domain-agnostic core model

Domain was left open, so the core is tasks-on-a-timeline: tasks with durations,
dependencies, eligibility, earliest-start and deadline windows; resources with
capacity and availability windows. Shift rosters, mission timelines, and asset
booking all express in these primitives.

Consequence: no domain-specific vocabulary anywhere in the code. If a domain
gets pinned later, it becomes a layer on top, not a rewrite.

## 2026-08-10 14:37 EDT — Zero third-party dependencies

Standard library only. No pydantic, no PyYAML, no click. Runs on any Python
3.9+ with no `pip install` step and no virtual environment required.

Rationale: the harness has to work on the first try on a machine that hasn't
been set up. Hand-written validation costs more lines than pydantic but removes
the entire dependency-install failure mode.

## 2026-08-10 14:38 EDT — Validation reports everything at once, never raises

`validate_plan` and `validate_schedule` return `(object_or_None, issues)`
instead of raising on the first problem. A malformed reply comes back as a list
of every issue found.

Rationale: fixing one error per round-trip is the slowest possible loop when a
model is generating the input.

## 2026-08-10 14:39 EDT — Severity split: error vs warn

Errors mean the schedule is not legal (double-booked resource, dependency
violated, wrong duration, outside horizon). Warnings mean it is legal but
suspect (start time off the time grid, task dropped without a stated reason).
Only errors set a non-zero exit code.

## 2026-08-10 14:40 EDT — Touching endpoints are not an overlap

A task ending at 09:00 and another starting at 09:00 on the same resource is
back-to-back, not a conflict. Capacity checking uses a sweep line that processes
end events before start events at the same instant.

## 2026-08-10 14:42 EDT — Fixed: timeline drew false overlap markers

The ASCII timeline marked `!` (overlap) whenever two assignments landed in the
same rendered cell. Because a cell spans several minutes, back-to-back tasks
shared a cell and rendered as conflicting — a legal schedule looked illegal.

Now the renderer tests actual interval overlap instead of cell collision, and
picks the character by which assignment fills more of the cell. Regression tests
cover both directions (back-to-back must not show `!`; true overlap must).

## 2026-08-10 14:42 EDT — Fixed: hallucinated task ids inflated the count

A reply containing a task id not in the plan produced "4 of 3 scheduled". The
summary now intersects scheduled ids with known plan ids. The unknown id is
still reported as an error by the reference checker; it just no longer corrupts
the metrics.

## 2026-08-10 14:43 EDT — Run outputs are not version controlled

`runs/` is gitignored except for `.gitkeep`. Run folders hold session artifacts,
including whatever gets pasted in, and do not belong in git history by default.
`runs/index.jsonl` is likewise local.

Reversal note: if run history should be shared or archived, remove the ignore
rule rather than copying files out.

## 2026-08-10 14:55 EDT — Model provenance recorded per run

Clarified that the model producing schedules is not necessarily the one Jake is
talking to while building this. The harness was already model-agnostic — no
client, no key, no vendor library — but runs had no record of *which* model
answered, which makes a diff between two runs ambiguous.

`ingest` now takes `--model NAME` and `--note TEXT`, stored in the run's
`meta.json` and echoed into the append-only index. `log`, `report`, and `diff`
all surface it. Provenance is optional; omitting it prints a reminder rather
than failing, so a quick run is never blocked.

Consequence: sending one plan to several models and diffing their schedules is
now a first-class workflow rather than something to reconstruct by memory.
