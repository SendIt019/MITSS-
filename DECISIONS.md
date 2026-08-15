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

## 2026-08-10 15:20 EDT — Split into /frontend and /backend

Restructured from a flat Python package into a standard two-tier application:
React (Vite) in `frontend/`, FastAPI in `backend/`. The existing core package
moved to `backend/mitss/` unchanged, along with its tests, inputs and runs.

The core keeps its zero-dependency rule; FastAPI and uvicorn are required only
by `backend/app/`. That boundary is deliberate — the scheduling logic stays
importable and testable without a web stack, and the command line still works.

## 2026-08-10 15:22 EDT — Text input: structured grammar first, model as fallback

Uploaded `.txt` files are parsed by a deterministic line-oriented grammar. If
the file does not match, the raw text plus the parser's complaints are handed to
the model to structure, and the structured result is validated exactly like a
hand-written plan before it is accepted.

Rationale: a deterministic parse gives exact line-numbered errors and identical
results every run. Reserving the model for the cases the grammar cannot read
keeps that property where it is available without making the strict format a
precondition for using the tool.

## 2026-08-10 15:24 EDT — Model harness: provider interface, manual by default

`LLMProvider` is an abstract interface. Two implementations ship: `manual` (the
default — produces no completion, so the operator carries the packet) and `http`
(posts to a configurable endpoint, in either the openai chat-completions shape
or a plain `{"prompt": ...}` body). Custom providers register by name.

Credentials come from the environment at call time, are sent once in the request
header, and are never logged, returned by the API, or written to a run folder.
`describe()` reports only whether a key is set. A test asserts that an API key
does not appear in an error message.

Rationale: the harness must never depend on a model being reachable, and it must
never become a place credentials accumulate.

## 2026-08-10 15:40 EDT — Chart colour carries status, not identity

In the timeline, identity is carried by the row (which resource) and the label
on the bar (which task), so the fill is free to encode status instead. Clean
blocks use the validated series blue; blocks with a constraint error use status
critical plus an icon, never colour alone.

Warnings deliberately do not recolour a bar: the warning yellow fails the fill
lightness band against the chart surface. Warned blocks carry an icon and appear
in the issues list. The two-fill palette was checked with the palette validator
and passes every gate in both light and dark mode.

## 2026-08-10 15:46 EDT — Fixed: overlapping bars hid the conflict

Bars on one resource row were drawn at a fixed vertical offset, so a later bar
painted over an earlier one. A double-booking — the single most important thing
to see — rendered as two neat adjacent blocks.

Bars are now assigned to lanes by greedy interval partitioning and the row grows
to fit, so concurrent work is stacked and visible. Resource-level errors such as
over-capacity also mark the row label, since they implicate the row rather than
any one bar. Verified by measuring rendered geometry in a browser, not by eye.

## 2026-08-10 16:10 EDT — REVERSAL: this is a prompt pipeline, not a scheduling tool

The domain question was answered "no preference" early on, and I read that as
"pick something reasonable and stay generic." I built a scheduling application
instead: a scheduling data model, a scheduling grammar, a constraint engine,
and a Gantt interface. That was the wrong abstraction at the wrong level.

What it actually is: a prompt goes in, hits the harness, an output comes out.
The harness renders the exact prompt, captures the exact output, and records
provenance. Everything scheduling-shaped was a domain sitting on top of a much
smaller idea.

Reversed. The generic pipeline is now the product, in `backend/pipeline/`.
Reused from the previous build: run storage and the append-only index, the
provider harness, output capture, the API and interface shell. Discarded from
the product (kept as an example): the scheduling model, grammar, constraints,
renderers.

Lesson for future sessions: "no preference" about a domain meant the domain
should not have existed, not that I should choose one.

## 2026-08-10 16:12 EDT — Capture only: the harness does not judge output

Explicit decision: no schema validation, no assertions, no automated checking
of what the model returns. The operator reads the output and decides.

This is a real constraint on the design, not an omission. It means the value of
the tool is entirely in organisation, provenance and comparison, so those are
where the effort goes.

## 2026-08-10 16:13 EDT — Verdicts: capturing the operator's judgement

Since accuracy is assessed by reading, each recorded output carries a verdict
(unrated, accurate, partly right, inaccurate) and a free-text note.

Rationale: with no automated checking, an unrecorded reading is lost the moment
the tab closes, and a version-by-model matrix would have nothing to display.
`unrated` is the default and is deliberately distinct from `accurate`, so
"not looked at yet" never masquerades as "checked and fine".

A cell in the matrix shows the worst verdict among its runs. A model that got
something wrong once is worth surfacing even if a later attempt passed.

## 2026-08-10 16:14 EDT — Prompt versions are immutable

Editing a prompt never overwrites. Saving writes the next version; runs freeze
the prompt text into their own folder at record time.

Rationale: the entire point of comparing across versions is that the older
result still reflects the older prompt. Mutable prompts would silently
invalidate every historical run. Saving text identical to the current version
is refused rather than creating a version that means nothing.

## 2026-08-10 16:16 EDT — Word-level diffing, not line-level

Output comparison uses difflib at word granularity with punctuation attached to
its word. A line diff on model prose reports a reworded sentence as one line
removed and one line added, which communicates nothing about what changed.

Consequence to be aware of: `passage.` becoming `passage briefly.` counts as
one token replaced by two, so raw added/removed counts read slightly high. The
rendering is what matters and it is exact — concatenating either side's spans
reproduces that side character-for-character, which is asserted in a test.

Diff highlighting carries a second channel — strikethrough for removed,
underline for added — so it does not depend on colour.

## 2026-08-10 16:18 EDT — Fixed: event log ordered a version before its prompt

`create_prompt` wrote the first version before logging the prompt's creation,
so the append-only index showed `version_added` for a prompt that did not
appear to exist yet. Creation is now logged first. Caught by a test asserting
the first event of a fresh store.

## 2026-08-10 16:20 EDT — Scheduling kept as a worked example

The scheduling package and its full test suite stay at `backend/mitss/`, with
`backend/examples/seed_scheduling.py` loading its generated packet into the
prompt library as a starting prompt.

Rationale: it is a good illustration of a heavily specified prompt, and it
still holds the provider harness that the pipeline imports. It is documented as
an example so nobody mistakes it for the product again.

## 2026-08-14 10:05 EDT — Input sets split from prompts

Question from Jake while looking at the prompt screen: where do the inputs go?
They had nowhere to go. The input data had to be pasted into the prompt box,
which meant every new document became a new prompt version.

That quietly broke what versions are for. v1 against v2 is only a fair test of
wording if the material is held constant; "same wording, different passage"
versions would have made the matrix meaningless.

Inputs are now a separate, reusable library. A prompt carries an `{input}`
placeholder, an input set carries the material, and the two render into the
final prompt. Versions track wording; inputs track material.

Consequences:
- The copy button copies the *rendered* prompt, never the template. Copying one
  thing and running another would be the worst possible bug in a tool whose
  entire value is provenance.
- A run freezes three texts, not one: the template, the input, and what they
  rendered into. Editing or deleting an input afterwards cannot rewrite what a
  past run was actually given — asserted by tests.
- Inputs are editable, unlike prompt versions, precisely because of that
  freezing.
- Rendering stays deliberately dumb: one placeholder, straight substitution, no
  expression language. A template engine would be a second thing to debug when
  a prompt misbehaves.
- An input supplied to a prompt with no `{input}` placeholder is appended at the
  end with a warning rather than silently dropped.

## 2026-08-14 10:08 EDT — The matrix says when it is not comparing like for like

With inputs in play, a version-by-model grid aggregated across several inputs
can mislead: v1 on an easy passage against v2 on a hard one says nothing about
the wording.

The matrix takes an optional input filter. Unfiltered it still renders — useful
for coverage — but reports `like_for_like: false` and the interface shows a
banner naming how many inputs are mixed in. Each cell also reports how many
distinct inputs it covers, and the grid lists version/model pairings never run.

## 2026-08-15 17:05 EDT — Rolling plain-text transcript

Jake asked whether an auto-fetched output could land in a single overarching
.txt file as well as the interface. It could not: the only whole-history file
was `index.jsonl`, which is machine-readable and not something anyone would sit
and read.

`data/transcript.txt` now accumulates a full block per run — timestamp, prompt
id and version, model, input name, source, duration, the complete rendered
prompt, the complete output, and the verdict. One file you can open, scroll,
grep, print, or hand to someone without the tool installed.

Append-only, like the index. A verdict set after the fact is appended as its
own line rather than edited into the block above, so the file stays a true
history. The consequence to know when reading it: a block can say "not reviewed
yet" while a later line records the verdict. It is a log, not a table.

Writing the transcript is best-effort and wrapped in a try. A failure to write
a convenience file must never lose the run itself, which is already on disk in
its own folder by that point.

Deleting a run removes its folder but leaves its transcript entry, matching how
the event index already behaves.

## 2026-08-15 17:20 EDT — SANDBOX.md described a feature that did not exist

Jake's own commit (cfc766d, written with Claude Code) added SANDBOX.md
documenting `MITSS_LLM_MODELS`, a **Run on ▾** model dropdown, and a `models`
list on `GET /api/llm`. None of the three existed in the code. Configuring
`.env` per those instructions would have produced no dropdown and silently sent
every request as the default model.

The verification block in that file exposed it: the stand-in endpoint echoed
`local-model` rather than the `team-70b` that was asked for.

Built the feature rather than deleting the documentation, since a model picker
is genuinely useful and the docs were a reasonable specification.

- `MITSS_LLM_MODELS` is a comma-separated list, order preserved, duplicates
  dropped, falling back to the single `MITSS_LLM_MODEL`.
- `LLMProvider.complete` takes an optional `model` override. The chosen model
  now reaches the request body, not just the run's label — labelling a run with
  a model the endpoint never saw would make every later comparison a lie. That
  is the regression test.
- `describe()` reports `models`, so the interface can offer the choice.
- The Prompt tab shows a **Run on** picker and a **Run** button when the
  provider reports models, and the old single-button fetch otherwise.

Note for anyone who wrote a custom provider before this: `complete` gained a
second parameter with a default, so existing subclasses keep working, but new
ones should accept and honour it.

## 2026-08-15 17:23 EDT — Fixed: interface never loaded at the advertised URL

The interface "never loaded" for Jake. Verified cause: with no `host` setting,
Vite resolves `localhost` and binds IPv6 `[::1]` only — reproduced by starting
the stock config and probing both families (`127.0.0.1:5173` refused,
`[::1]:5173` answered). Every URL the project advertises — dev.sh's banner,
the README — says `http://127.0.0.1:5173`, so following the printed URL hit a
connection refused.

Fix: `server.host: '127.0.0.1'` in `frontend/vite.config.js`, so the server
binds exactly the address the banner prints. Confirmed by a clean `./dev.sh`
run: frontend 200 on `127.0.0.1:5173`, `/api/health` and `/api/llm` good on
`:8000`, and the `/api` proxy through Vite reaching the backend.

Related trap found while verifying: stale dev servers from an earlier session
were still holding :8000 and :5173, so a new `./dev.sh` failed to bind — uvicorn
logged "Address already in use" and Vite silently moved to :5174, where the
proxy still worked but nothing advertised the port. If the app misbehaves
strangely, check `lsof -nP -iTCP:5173 -iTCP:8000 -sTCP:LISTEN` first.

## 2026-08-15 17:28 EDT — Restored the M-SALUTE feature clobbered by delivered files

Commit 1537b75 (M-SALUTE structured inputs, mission-file default prompt, title
rename fixes) is an ancestor of main, but the three commits delivered from
another session (cfc766d, 3d2f0e2, 73023a1) were built from files that predated
it, so committing them regressed `App.jsx`, `Inputs.jsx`, and `styles.css` to
pre-M-SALUTE content. `salute.js` survived, orphaned — nothing imported it.

Merged forward rather than reverting: `Inputs.jsx` restored verbatim from
1537b75 (HEAD's copy was byte-identical to the pre-M-SALUTE version, so nothing
newer was lost), and the M-SALUTE sample template, title focus/rename fixes,
and salute CSS grafted into `App.jsx`/`styles.css` alongside the transcript
links and model picker those commits added.

Also reconciled 8cbfdd9 (Jake's model dropdown, likewise clobbered then rebuilt
by 73023a1): the rebuild covered everything except the success toast naming
the model that ran; that line is now restored. The rebuild's extras — fallback
fetch button when no models are configured, run disabled while the prompt has
unsaved edits — are kept.

Verified in a real browser (headless Chrome via Playwright): all seven fields
render in order, an input created through the form appears in the library, and
reopening it parses the stored block back into the fields. No console errors.
