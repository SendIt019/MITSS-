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

## 2026-08-20 07:09 HST — Dark ops console theme

Jake picked a permanent dark direction for the interface over a refined light
theme or a light/dark toggle, so the light palette and the automatic
prefers-color-scheme flip are gone: one committed look, dark slate surfaces,
monospace chrome (card titles, tabs, labels, metadata), and a single amber
accent reserved for interactive state — active tab, focused field, selected
row, primary action. Amber is chrome only, never a data colour.

Verdict and status colours are unchanged and were re-validated against the new
surface (#121722): all four clear 3:1 contrast. The validator's categorical
checks do not apply to them — verdicts never appear as adjacent series, and
each ships a distinct glyph and label, so meaning never rests on colour.

Two real bugs surfaced while verifying in the browser, both invisible in the
code review and obvious on screen:

- The generic `button:hover` rule (specificity 0-2-1) outranked every
  specialised button state at 0-2-0 — tabs, sidebar items, and run rows are
  all `<button>`s, so the active tab's amber underline degraded to a muddy
  translucent mix whenever the mouse was over it. Diagnosed from the computed
  colour's alpha (0.614 = 55% + 45%×0.14, exactly the hover colour-mix).
  The generic rule now excludes `.tab`, `.prompt-item`, and `.run-row`.
- A first screenshot captured mid-transition and looked like the underline was
  stuck on the previous tab; it was not — always settle transitions before
  reading computed styles.

Data note: screenshots were staged against a scratch backend (`MITSS_ROOT`
pointed at a temp dir) so demo runs never touch the real append-only index and
transcript.

## 2026-08-20 07:20 HST — Review-readiness pass

Jake asked for the repository to be cleaned up for external review. Every
change below was verified by running it, per the house rule:

- `backend/requirements.txt` gains `httpx2` — starlette's TestClient needs it,
  and a reviewer following the README's install-and-test steps hit 39 API-test
  errors without it (found the hard way on 2026-08-15).
- The `test_api.py` import guard now catches the `RuntimeError` starlette
  raises when httpx2 alone is missing, so "the API tests skip themselves on a
  bare install" (backend/README.md) is true for partial installs too. Verified
  on a clean-environment interpreter: 184 ran, 39 skipped, rest pass, zero
  installs.
- CI added (GitHub Actions): backend tests on an installed environment,
  frontend production build, and a bare-interpreter job with no pip install —
  that third job turns the "core has no third-party dependencies" invariant
  from a claim in CLAUDE.md into something a PR cannot silently break.
- `Timeline.jsx` deleted: the last scheduling-era component in the frontend,
  imported by nothing since the 2026-08-10 reversal.
- `MITSS_LLM_MODELS` added to `.env.example` — README and SANDBOX.md both
  document it, but the file users actually copy never mentioned it.
- `frontend/package.json` version aligned to the API's 0.3.0.
- `.claude/settings.local.json` untracked and gitignored: per-machine
  assistant permissions, not shared configuration.
- GitHub repo description updated from the seed text ("I/O for Box").

Flagged, not done: the repository has no LICENSE (a legal choice, Jake's to
make) and the GitHub repo name carries a trailing hyphen (`MITSS-`), a rename
only the owner can do.

## 2026-08-20 07:45 HST — Fixed: a ".." id could delete the append-only files

A full-code audit before external review found that ids from URLs were joined
straight into filesystem paths. `DELETE /api/inputs/%2e%2e` resolved
`data/inputs/..` to `data/` itself and removed `index.jsonl` and
`transcript.txt` — the two files the product promises never to rewrite —
before erroring on the first subdirectory. Same mechanism through
`/api/runs/{id}` and every other id-taking route. Confirmed end-to-end
against a throwaway root before fixing.

Every id the store mints comes from `slugify()` or `stamp()`, so the fix is an
allowlist at the three path builders (`prompt_dir`, `input_dir`, `run_dir`):
an id outside `[a-z0-9-]` raises `NotFound`, indistinguishable from a miss, so
a probe learns nothing. Regression tests cover the store layer (hostile ids
across get/delete for all three kinds) and the HTTP layer (encoded `..` ids
return 404 and the index survives).

Also fixed from the same audit: the diff tokenizer (`\S+\s*`) dropped a
leading whitespace run, so an output starting with a blank line rendered in
Compare with that line silently stripped — contradicting the documented
"concatenating one side reproduces it exactly" contract. Leading whitespace
is now its own token; the reconstruction test gained a leading-whitespace
case. Suite is 188 tests, all passing.

## 2026-08-22 14:24 HST — Jake's full review: ten verified findings, all resolved

Jake ran /code-review across the last five commits; eight finder angles plus
a verification pass produced ten findings, every one empirically confirmed.
The fixes, worst first:

- **M-SALUTE round-trip gate.** Three findings (preamble text silently
  dropped, an embedded header-looking line shuffling content between fields,
  CRLF inputs reading as dirty on open) were one root cause: the structured
  form would open text it could not write back. `parseSalute` now succeeds
  only when `assembleSalute(parse(text)) === text` byte-for-byte; anything
  else opens in the raw editor where nothing can be dropped or rewritten.
  Verified under node for all four cases and in the browser.
- **Hover specificity, round two.** The `:not()` exclusion chain added on
  2026-08-20 raised the generic button hover to specificity 0-5-1 — `:not()`
  arguments count — so it beat `button.primary:hover` and friends: hovered
  primary buttons went dark-on-dark. The exclusions now sit inside
  `:where()` (adds no specificity), and the two selected-state rules gained
  a `button.` prefix to win by source order. Lesson recorded: a specificity
  fix can reintroduce the bug one level up; verify hover states in the
  browser, not just resting states.
- **Escape in the title input committed the rename it should cancel** —
  blur() fires synchronously with the stale draft closure. A ref flag now
  marks the escape before blurring. Verified in the browser: server name
  untouched.
- **The documented `MITSS_LLM_MODELS` example broke dev.sh** — unquoted
  comma-space value under `set -e` sourcing exits 127 before either server
  starts. Quoted in `.env.example`, README, and SANDBOX.md.
- **Whitespace tokens counted as words** in diff stats (a consequence of the
  08-20 tokenizer fix). Word counts and similarity now ignore
  whitespace-only tokens; `identical` compares the texts themselves.
- **Ids outside the minted alphabet**: listing showed hand-made directories
  that lookup then rejected. Decision: the store uniformly serves only ids
  it could have minted — `list_*_ids` now applies the same filter as
  `_safe_id`, so a hand-restored `My_Prompt` directory is invisible (not
  half-visible) and its files remain untouched on disk; rename the directory
  to a slug to bring it into the library.
- **Drift tripwire**: a test now creates prompts/inputs/runs with hostile
  names (over-length, unicode, punctuation, collision suffixes) and reads
  them back, so the mint pattern and the check pattern cannot drift apart
  unnoticed.
- **The API-test skip guard** now re-raises RuntimeErrors that are not the
  known missing-httpx2 case, and the full-install CI job imports TestClient
  as its own step — a broken environment fails loudly instead of skipping
  40 tests green.

Suite is 190 tests, all passing; frontend builds; all fixes verified in a
real browser against a scratch data root.

## 2026-08-31 17:55 EDT — The team layer: registry, batch, digest, review queue

The harness assumed one operator and one model configuration. The team now
sends models in, so four things were built, all on the existing invariants:

- **Model registry** (`data/models/`, `/api/models`, Models tab). A teammate
  hands over connection details — name, owner, URL, body shape — and their
  model becomes a run choice, a matrix column, and part of batch runs.
  Decision: a registration can never hold a credential. `key_env` stores the
  NAME of an environment variable, the service layer rejects values that do
  not look like one (a pasted `sk-…` key is refused with an explanation),
  and the API reports presence only. Decision: the name is immutable after
  registration — runs are labelled with it, and renaming would detach every
  recorded run from its column. Deleting a registration leaves its runs
  untouched.
- **Batch runs** (`/api/batch`, "Run all" button) — the known gap closed.
  Sequential on purpose: stdlib urllib, no async machinery, and each result
  reports ok/failed so one dead endpoint does not sink the batch.
- **Digest** (`/api/digest`, Digest tab). Everything rolled up by prompt,
  version and model, in JSON and as a plain-text page (`?format=text`)
  mirroring the transcript's philosophy: readable without the tool,
  pasteable into a model as context. It compiles the operator's verdicts and
  judges nothing itself, so capture-only stands.
- **Review queue** (`verdict` filter on `/api/runs`, "To review" toggle on
  the Outputs tab) — the other known gap closed.

Interface decision: tabs became global navigation. Models, Digest and Inputs
are library-level, not prompt-level, so they now work with no prompt
selected; Prompt/Outputs/Matrix/Compare still ask you to pick one. The
provider `Run on` picker (env-configured) and the Registered picker coexist:
the env path is one shared endpoint, the registry is per-teammate.

`HttpProvider` gained a `key_env` parameter (default `MITSS_LLM_API_KEY`,
so existing configuration is unchanged) — a provider built from a stored
registration reads that variable at call time, same rule as before.

Verified: 211 tests pass (21 new — registry storage and API, key rejection,
key non-leak, batch against a real stub server including auth header and
body-model assertions, digest build/text/empty, verdict filters); every new
endpoint exercised with curl against a live uvicorn on a scratch root, with
the stub echoing back which model name and auth header it actually received.
The frontend builds clean. NOT yet verified: the restructured interface in a
real browser — no browser was available this session; recorded as a known
gap rather than claimed.

## 2026-09-22 11:12 CDT — granite-4.0-h-tiny crash: batched generation past ~12k tokens, not the model swap

Diagnosed outside the harness on port 8081 with the exact rendered prompt
frozen in run `20260922-090636-lite-comms-plan-from-m-salute-v3` (input set
02_cav_division_120h, 3,664 tokens). mlx 0.32.2 / mlx-lm 0.31.3 are the newest
releases on PyPI, so an upgrade was never on the table.

| case | setup | result |
| --- | --- | --- |
| a | `mlx_lm.generate`, max-tokens 8000 | OK, 8000 tokens at 100 tok/s, peak 7.2 GB |
| b | server started with granite, `--max-tokens 8000` | OK, 8000 tokens in 94 s |
| c | same, `--max-tokens 32768` | **crash**: `[metal::malloc] Resource limit (499000) exceeded` in `_generate`, a few minutes in |
| d | server started with gemma-3-12b, one gemma request, then granite at 32768 | **crash**, same error, mid-generation |
| e | server at 32768, request carries `max_tokens: 8000` | OK |
| f | server at 32768, request carries `seed: 0` (non-batched path) | OK, all 32768 tokens in 357 s |
| g | repeat of b | OK |
| h | server at 16000 | **crash**, same error, ~2.5 min in |

Reading: the error is MLX's cap on the number of live Metal buffers (499,000),
not on bytes. Only the server's batched generator (`BatchGenerator`, used
whenever a request has no `seed`) accumulates buffers per generated token for
this hybrid Mamba model; a fresh or swapped server makes no difference, and
the 8000 cap simply stops before the limit is reached. Granite's greedy
output loops (`ASSUMED,ASSUMED,...`) so it always runs to whatever cap it is
given, which is why raising the server cap to 32768 turned a slow run into a
dead generation thread. After the crash the HTTP thread keeps answering, so
every later request hangs until the client times out — the "0% GPU, no 502"
symptom.

Decision: no server flag and no upgrade. The fix is per-model settings
(below): give granite `max_tokens: 8000` on its registration; a `seed` also
works by forcing the non-batched path but costs the batching for everyone
else, so it is the fallback, not the default. Root cause inside mlx-lm is not
chased further here — it belongs upstream.

## 2026-09-22 11:12 CDT — Generation settings live on the registration; every run keeps a snapshot

`HttpProvider` hardcoded `temperature: 0` and sent no `max_tokens`, so a
thinking model ran greedy (which Qwen warns can loop) to the server's cap, and
the 502 for a 172-second qwen3.5-9b run was really a client timeout.

- A registered model now carries `settings`: `temperature`, `top_p`, `top_k`,
  `min_p`, `presence_penalty`, `max_tokens`, `seed`, `timeout`,
  `chat_template_kwargs` (for `enable_thinking`). Every name was checked
  against the installed `mlx_lm/server.py` (0.31.3) and is honoured there;
  `min_p` and `presence_penalty` are included because Qwen's published
  thinking-mode values use them. Blank means today's defaults: temperature 0,
  no cap beyond the server's, `MITSS_LLM_TIMEOUT`. Edited on the Models tab
  or via `settings` on `POST/PATCH /api/models`; a `PATCH` replaces the whole
  block, `{}` clears it. Bad values are refused with the field named
  (`temperature must be at least 0`, `unknown setting 'temprature'`).
- Each run records the settings the request actually carried, plus the
  client timeout, in `run.json`, in the API, and as a `settings:` line in
  `transcript.txt` (`temperature=1.0  max_tokens=2048  enable_thinking=false
  timeout=600s`). It is a snapshot, not a reference: editing the registration
  later cannot rewrite what an old comparison was made with. Pasted runs and
  runs recorded before today have `settings: null` and no transcript line;
  old `run.json` and `model.json` files load unchanged (tested, and checked
  against the real data directory).
- Registrations are gitignored working data (`backend/data/models/`), so the
  values for qwen3.5-9b are recorded here. They come from the Qwen/Qwen3.5-9B
  model card (the local folder has no generation_config.json and only an
  mlx-community stub README): thinking mode, general tasks —
  `temperature 1.0, top_p 0.95, top_k 20, min_p 0.0, presence_penalty 1.5`,
  recommended output length 32,768; non-thinking —
  `temperature 0.7, top_p 0.8, top_k 20, min_p 0.0, presence_penalty 1.5`.
  Thinking is on by default in the chat template and turned off per request
  with `chat_template_kwargs: {"enable_thinking": false}`. Suggested
  registration: the thinking-mode values, `max_tokens 32768`, `timeout 1800`.
  For granite-4.0-h-tiny: `max_tokens 8000` (see the entry above). Not
  applied to the live registrations by the assistant — that is data.
- Verified live against qwen3.5-9b on port 8081 through the FastAPI app on a
  scratch root: thinking off answered in 3.8 s, thinking on took 77.7 s for
  the same question with the same sampling, and both snapshots landed in
  run.json and the transcript. Note for later: mlx_lm.server returns a
  thinking model's reasoning in a separate `reasoning` field, which the
  harness does not record — the output is the answer only.

## 2026-09-22 11:12 CDT — One error per cause: 504 timeout, 502 refused, 502 server error; timeouts validated

`service.py` turned every provider failure into a 502 with whatever urllib
said, so a timeout, a server that was not running, and a crashed generation
thread were indistinguishable in the UI.

- `LLMTimeout` → **504** "did not answer within Ns — it may still be
  generating (raise the timeout), or mlx_lm.server may be stuck: if the GPU
  is idle, restart it". `LLMUnreachable` → **502** "could not connect ...
  (connection refused) — is mlx_lm.server running?". `LLMServerError` →
  **502** "model server returned HTTP 500: <the server's own error text>",
  quoted from the response body (never the request, which carries the key)
  and trimmed to 200 characters. Bad configuration (`LLMConfigError`) → 500
  naming the variable. The UI shows the message as-is in its error banner.
- A reply with `reasoning` but no `content` (a thinking model that hit
  max_tokens mid-think) is now "the model produced reasoning but no answer
  (finish_reason: length)" instead of "could not find completion text".
- `MITSS_LLM_TIMEOUT` and a registration's `timeout` must be a positive
  finite number of seconds: `0`, negatives, `inf`, `nan`, blanks and words
  are refused at provider construction, so `/api/llm` reports the problem
  and generate fails before a request is sent. Previously a bad value fell
  back silently to 120 and `0` would have meant "no wait".
- Verified live: 504 at a 2 s timeout against qwen mid-generation, 502 for a
  port with nothing on it, 502 quoting the server's 404 text for a model path
  that does not exist.

Gates: 236 backend tests (25 new), `compileall` clean, frontend `npm run
build` clean. The Models tab form was verified by build and by the API it
calls, not eyeballed in a browser.

## 2026-09-22 11:12 CDT — Stuck-server detection and a start script: proposed, not built

Both are designs for Jake to approve first; see the session report. The
stuck case (HTTP thread alive, generation thread dead) is only detectable
by asking for a token, so the proposal is a one-token preflight with a short
deadline, and its cost is stated per run rather than hidden in a default.

## 2026-09-22 11:40 CDT — Preflight probe, reasoning captured, one-command server start

Jake asked for all three after the session report.

- **Preflight.** Before each provider run, `HttpProvider` sends a one-token
  request for the same model with its own deadline
  (`MITSS_LLM_PREFLIGHT_TIMEOUT`, default 90 s, `0` disables; openai bodies
  only). A dead generation thread now surfaces as **503** "model server is
  stuck, restart mlx_lm.server ... (the prompt was not sent)" instead of a
  hang. Measured against qwen3.5-9b on 8081: 0.16 s per probe when the
  model is loaded; a swap's load time lands in the probe rather than the
  run, which is why the deadline is 90 s and not 5. Verified live: after
  crashing granite's generator the known way, a generate with a 1800 s
  timeout returned 503 in 90 s.
- **Reasoning.** mlx_lm.server returns a thinking model's chain of thought
  as `message.reasoning`; the harness now keeps it verbatim, apart from the
  output: `reasoning.txt` in the run folder (only when present), `reasoning`
  on the run detail and a `reasoning_characters` count in lists, a fold on
  the run panel, and a `REASONING:` section between PROMPT and OUTPUT in
  the transcript. Capture-only still holds: it is recorded, not judged. A
  reply with reasoning and no answer (the cap ran out mid-think) is now
  recorded as a run with an empty output and the reasoning attached, rather
  than raised as an error — the reasoning is the evidence that max_tokens
  was too low. Verified live: a thinking-on qwen run recorded 16,948
  characters of reasoning beside a 304-character answer.
- **`scripts/start_model_server.sh [model] [port]`** runs caffeinate plus
  mlx_lm.server with the standard flags (defaults gemma-3-12b, 8080,
  `--max-tokens 32768`), refuses a missing model folder or a port already in
  use, and takes `MITSS_MODELS_DIR`, `MITSS_MODELS_ENV`,
  `MITSS_SERVER_MAX_TOKENS` overrides. Used for every live check above.

Gates: 246 backend tests (10 new), compileall clean, frontend build clean.

## 2026-09-22 12:30 CDT — Two external reviews: what was taken, what was refused

Reviewed `CLAUDE_CODE_MITSS_REVIEW.md` and
`MITSS_Code_Architecture_Review_Claude_Code.md`. Every finding was checked
against the code before anything changed; several did not survive that check.

**Taken.**

- **Secret redaction (CONFIRMED, and a regression from this morning).** The
  `_server_said` helper added earlier today quotes the model server's error
  body back to the operator. A server or proxy that echoes the
  `Authorization` header it received — "Invalid authorization: Bearer sk-…" —
  therefore put the key in the API response, the UI banner and the backend
  log. Reproduced with a stub that echoes the header. There is now one
  `redact()` in `mitss/llm.py`, applied to the quoted body before it is
  raised, and a regression test with a deliberately echoing server. The older
  test only proved the *request* was not echoed, which is why this got
  through.
- **Provider telemetry.** Runs now keep `usage` exactly as the server
  reported it: `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `finish_reason`, `model_reported`, plus a measured `tokens_per_second`.
  Nothing is estimated — a server that reports no counts stores `usage: null`
  rather than a guess from word counts. `finish_reason: length` is surfaced
  in the UI with a note that the answer is probably truncated, which is
  exactly the granite failure mode from this morning. The rate is measured
  against the generation request alone, excluding the preflight probe, so a
  model that had to be loaded is not recorded as a slow one.
- **Input fingerprint.** Every run stores `input_sha256` of the text it
  actually used. Inputs stay editable by design, so two runs sharing an
  `input_id` are not necessarily like-for-like; the hash is how that is told
  apart without reading both. This is the cheap half of the review's
  "version the inputs" proposal and it needs no migration — old runs simply
  have an empty hash.
- **Batch observability.** `batch_generate` prints one line per model to the
  backend terminal (`[batch 3/12] qwen3-14b: recorded in 94.2s`) and each
  result carries `elapsed_ms`; the UI flash reports the total. A batch is a
  single HTTP request that can run for an hour, and it was silent.
- **`.gitignore` hardening.** `backend/.env.*` with `!backend/.env.example`.
  No env file other than the example is or ever was in git history (checked
  with `git log --all --diff-filter=A`), so the review's "remove tracked
  backup files" is not the situation here — but one `cp .env .env.bak` away
  from being it.
- **Documentation.** `backend/README.md` described `mitss/` as "the core" and
  never mentioned `pipeline/`, which is the actual product; corrected.
  `.env.example` said `MITSS_LLM_TIMEOUT=120`, which cuts a local 20B+ run
  off and looks like a model failure; now 900 with a note. Hardcoded test
  counts removed from `CLAUDE.md` and `README.md` rather than updated again.
  `SANDBOX.md` now states the trust boundary plainly: one operator, one
  machine, no authentication, and the backend will call whatever URL a
  registration names.

**Refused, with reasons.**

- **`httpx2>=2` is not a typo.** The review called it P0 and wanted
  `httpx>=0.27`. `httpx2` is the Pydantic team's successor to httpx by the
  same author, and starlette 1.6 does `import httpx2 as httpx` first,
  falling back to `httpx` only if it is missing. Proved by building a clean
  virtualenv from `requirements.txt` alone: httpx2 2.13.0 installs, httpx is
  absent, `TestClient` imports, and the whole suite passes. Changing it would
  have been a downgrade. A comment now says so, so the next reviewer does not
  re-raise it.
- **Full input versioning.** The stated problem is real; the sha256 above
  answers it for a fraction of the cost. Revisioned inputs would touch the
  store, the API, the matrix and the UI, and would need a migration for
  existing data. Worth doing deliberately, not as a side effect of a review.
- **Configuration fingerprint.** The settings snapshot plus `input_sha256`
  already contain everything a fingerprint would hash, so it would be a
  convenience field with no consumer until the matrix warns on mixed cells.
  Add it with that warning, or not at all.
- **Deterministic output validators.** `CLAUDE.md` says capture-only: no
  automated scoring or schema checks on model output unless Jake asks for
  them. The review's own "keep the human verdict separate" is the right
  design *if* this is ever wanted, but it is his call, not a reviewer's.
- **Batch skip-heavy / pause-between-models.** The premise is that the local
  server keeps the previous model resident on a 24 GB machine. mlx_lm.server
  does not: `ModelProvider._load` clears `self.model` before loading the next
  one ("Remove the old model if it exists"), so models swap rather than
  accumulate. `POST /api/batch` already accepts `model_ids`, so "run all the
  small ones" is available today without a new mechanism. The first review
  was written against an LM Studio configuration at port 1234, which is not
  the current setup.
- **Parallel execution, atomic writes, SQLite, a single version source.**
  Single-user localhost; the reviews agree these are premature. Sequential
  batch stays.

Gates: 260 backend tests, compileall clean, frontend build clean, and the
suite also passes in a clean virtualenv built only from `requirements.txt`.
Telemetry and the input hash verified live against qwen3.5-9b on port 8081.

## 2026-09-22T17:50:28-05:00 — Streamed requests with an idle-gap timeout, standard library only

A non-streamed request makes mlx_lm.server write nothing, not even headers,
until the whole completion exists. A long generation therefore looked like a
dead socket: the 1800 s client timeout fired mid-generation and the server
logged a BrokenPipeError when it finally tried to answer. Prompt processing
was not the problem (3922 tokens in ~4 s).

- openai bodies now carry `stream: true` and `stream_options:
  {"include_usage": true}`. The SSE deltas (content, reasoning,
  finish_reason, usage, model) are accumulated into one chat-completion
  document and parsed by the same code as before, so `Completion`,
  `run.json`, `reasoning.txt` and the transcript keep their shape. A server
  that ignores `stream` and answers with plain JSON still works.
- `MITSS_LLM_TIMEOUT` and a registration's `timeout` now mean **idle
  seconds**: the longest gap allowed between two received bytes (default
  120). It is the socket timeout after connect. mlx_lm.server's `: keepalive`
  lines during prompt processing reset it too. Connecting has its own 10 s
  limit (`socket.create_connection` via `http.client`).
- httpx was not added: `backend/mitss/` is stdlib-only by design. urllib was
  replaced by `http.client` because urllib cannot set separate connect and
  read timeouts. Two behaviour changes follow: redirects are no longer
  followed, and `HTTP(S)_PROXY` variables are no longer honoured. Neither
  applies to a model server on 127.0.0.1.
- A stream that closes without `[DONE]` or a finish_reason is reported as a
  dropped connection, not as a short answer.
- Verified live against llama-3.1-8b on 8080: with a 3 s idle limit, an
  800-token generation that took 31.8 s completed. The ~28 s model load
  landed in the preflight, not in the run.

## 2026-09-22T17:50:28-05:00 — max_tokens on every request: MITSS_MAX_TOKENS, default 1024

Every generation request sends `max_tokens`: a registration's own setting,
otherwise `MITSS_MAX_TOKENS`, otherwise 1024. A bad value (0, negative,
non-integer) is refused at provider construction. The preflight probe stays
at `max_tokens: 1`. A run's settings snapshot now includes `max_tokens`
where it used to show nothing, because it is now actually sent. Two tests
changed for that reason: `test_default_body_is_temperature_zero_and_the_default_token_cap`
(was `..._and_nothing_else`) and `test_run_without_settings_still_serves_them_as_null`.

1024 will cut off thinking models (qwen3, gpt-oss) that have no
`max_tokens` of their own. Truncation is made visible (entry below) rather
than hidden by a larger default.

## 2026-09-22T17:50:28-05:00 — Truncation at the token cap is visible in batch output

`run.json` already records `usage.finish_reason` (since cadfb23), and the
transcript already shows `stopped: length`. Streaming keeps both. No field
was added to `run.json`; its top-level keys are unchanged (checked on a
live run). Each successful batch result now carries `finish_reason` and
`truncated` (true when finish_reason is `length`), and the backend log line
says `TRUNCATED at the token cap`. A cut-off answer must never be scored as
a complete one.

## 2026-09-22T17:50:28-05:00 — Local model names are sent as folder paths, checked first

mlx_lm.server loads whatever path a request names. A bare folder name is
resolved against the server's working directory and comes back HTTP 404
(re-confirmed live today). For endpoints on this machine only (127.0.0.1,
localhost, ::1), a model name that is not already absolute is sent as
`$MITSS_MODELS_DIR/<name>`. `MITSS_MODELS_DIR` defaults to
`~/Desktop/models`, expanded at runtime; no home directory is hardcoded.
Remote endpoints are sent exactly what they are configured with. Before any
request, the folder must exist and hold a `config.json` that parses;
otherwise nothing is sent and the model is reported unavailable (409 for a
single run, a skipped-with-reason result in a batch). The run label is
unchanged (the registration's name).

All twelve registrations already store absolute paths, so the resolution
matters for bare names (for example an environment-configured model list).
Caveat: a non-mlx server on localhost that takes bare names (Ollama, for
instance) would now be sent a path. No such endpoint exists today.

Tests talking to a 127.0.0.1 stub now point `MITSS_MODELS_DIR` at a
throwaway folder (`tests/model_folders.py`). Five assertions on the
request's `model` now expect the resolved path; the value sent really did
change.

## 2026-09-22T17:50:28-05:00 — A 404 is permanent for that model in that batch; one retry for connection errors and timeouts

There was no retry code in MITSS before this. The "two 404s four seconds
apart" were two separate requests, not a retry. mlx_lm.server 0.31.3 turns
any exception raised while starting a generation, a failed model load
included, into HTTP 404 (`server.py`, `handle_completion`). So a 404 is a
fact about the model, not a passing fault.

- Retry policy: a connection failure (refused, reset, stream closed early,
  connect timeout) or an idle timeout gets exactly one retry after a 1 s
  pause. An HTTP error status is never retried. The preflight retries a
  connection failure once; a preflight timeout is still "stuck" (503), with
  no retry.
- A 404 or a failed folder check marks the model unavailable for the rest
  of the batch, keyed by (url, model). A second registration of the same
  endpoint and model is skipped with the first one's reason, and the sweep
  continues. Results carry `unavailable: true` and `reason`; skipped ones
  also carry `skipped: true`. The response adds a `skipped` count. `failed`
  still counts every result that did not record, skipped ones included, so
  the interface's existing "N failed: ..." flash lists them.

## 2026-09-22T17:50:28-05:00 — mistral-nemo-12b quarantined: no supported way to pass fix_mistral_regex

transformers warns that the tokenizer in `~/Desktop/models/mistral-nemo-12b`
loads with an incorrect regex and suggests `fix_mistral_regex=True`. The
server loads the tokenizer, not MITSS. What was checked in mlx_lm 0.31.3:
`mlx_lm.server --help` has no tokenizer-config option, and
`ModelProvider.__init__` builds the tokenizer config from only
`--trust-remote-code` and `--chat-template` (`server.py` 320-324). There is no
supported way to pass the flag, so nothing under `~/Desktop/models` was
edited.

- Registrations gain a `quarantine` field: a reason string, where empty
  means not quarantined. A quarantined model is skipped by every batch
  (`skipped`, `quarantined: true`, `reason`), and a single run against it
  is refused with 409. `PATCH /api/models/{id}` accepts
  `quarantine`: a reason sets it, `""` lifts it, and omitting it leaves it
  alone (max 500 characters). Old `model.json` files without the field
  load as not quarantined. The interface does not show the field yet
  (backend-only task).
- The field was set on `backend/data/models/mistral-nemo-12b/model.json` by
  hand, under Jake's explicit, scoped exception, rather than through the
  API: `update_model` would also have rewritten `updated_at` and appended
  an event, which the exception did not cover.

## 2026-09-22T17:50:28-05:00 — Stored registration timeouts converted to idle seconds

Under the new meaning, the 1800 on qwen3.5-9b ("wait 30 minutes between
tokens") would amount to no timeout at all. It was reset to 120 by hand, with
the same scoped exception. It is the only registration that stored a
timeout; the other eleven have none and use `MITSS_LLM_TIMEOUT`, so nothing
was added to them. Values were not reinterpreted in code (a proposal to cap
"large" stored values automatically was rejected): a stored value means
what it says.

## 2026-09-22T17:50:28-05:00 — Gates for this branch

unittest (284, 24 new), `compileall`, and pytest (via `uvx`, with fastapi,
python-multipart and httpx2) all pass. So does the Python 3.9
no-dependencies suite (71 API tests skip, as in CI). ruff was run on the
changed files only, as agreed: 282 findings, down from 285 at the branch
point, and none on added lines except one I001. That I001 is the existing
`redact`/`parse_timeout` order in `tests/test_llm.py`'s import block,
deliberately left alone. `except (socket.timeout, TimeoutError)` keeps both
names with a `noqa: UP041`, because the alias only exists from Python 3.10
and CI runs the core on 3.9.

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

## 2026-09-29T12:42:32-05:00 — Matrix runner Step 0: survey done, branch and layout chosen

The survey is `docs/runner/SURVEY.md`. The planning entries above, from
10:47 to 12:13 CDT, were appended verbatim from
`docs/runner/DECISIONS-planning.md`, in that file's own timestamp format.

- Work happens on `feat/matrix-runner`, branched from `a60b66e`. `main` lacks
  the three commits the runner builds on (per-model settings, preflight,
  streaming, quarantine and 404 handling).
- The runner is `backend/run_matrix.py`, standard library only, on Python 3.9.
  It calls `service.generate_run` once per cell. Nothing is extracted from
  `batch_generate`, which stays unchanged: its loop does different
  bookkeeping (per model, not per cell).
- The manifest and `results.jsonl` live in
  `<data root>/data/matrices/<matrix_run_id>/`, found through
  `service.store().data_dir`. The store only lists its four known
  subfolders, so it never sees them.
- Retries are in `HttpProvider.generate` and its preflight, not in
  `batch_generate`, as the spec had said. The spec was corrected.
- Skips after an unavailable model are keyed by the served `(url, model)`,
  as in `batch_generate`. Matrix files use registration IDs; console output,
  runs and the transcript use names. Gemma 4's quantization is reported as
  "mixed" (4-bit with 8-bit router projections).

## 2026-09-29T12:42:32-05:00 — Runner environment goes through a wrapper script

Only `dev.sh` loads `backend/.env`, so a runner started from a plain terminal
would not see `MITSS_LLM_TIMEOUT`, which `.env` sets. Separately, a cold load
of a 14 GB model happens inside the one-token preflight, whose 90 s default
could be too short, and a registration cannot change it.

- `scripts/run_matrix.sh` loads `backend/.env` the way `dev.sh` does, sets
  `MITSS_LLM_PREFLIGHT_TIMEOUT` to `MITSS_MATRIX_PREFLIGHT_TIMEOUT` or 300,
  then runs `backend/run_matrix.py` with the same arguments. Built in Step 1.
- `run_matrix.py` never opens `.env`.
- `check` prints the data root and the effective `MITSS_ROOT`,
  `MITSS_LLM_TIMEOUT`, `MITSS_LLM_PREFLIGHT_TIMEOUT`, `MITSS_MAX_TOKENS`
  and `MITSS_MODELS_DIR`, and nothing else from the environment.
- The three lineup registrations also carry `max_tokens` and `timeout`. Step 1
  prints them, including a `PATCH` for `llama-3-1-8b`, which has no settings
  block today.

## 2026-09-29T12:42:32-05:00 — Resume reconciles interrupted cells from index.jsonl

A run is recorded inside `generate_run`. A Ctrl-C after that but before the
runner writes its result line would leave a run that resume does not know
about, so resume would record the cell a second time.

- Before each call the runner appends a `started` line to `results.jsonl`.
- For a cell with a `started` line and no result, resume looks in
  `index.jsonl` for a matching `run_recorded` event after the start time and
  adopts that `run_id` instead of calling the model again. This is read-only;
  nothing in `data/` is rewritten. Every other cell is decided from
  `results.jsonl` alone.
- The first Ctrl-C lets the current cell finish, then stops cleanly. A second
  one stops at once. Both write the summary and exit 2.

Spec §2, §5.1, §5.3, §5.4, §6, §7, §8 and §9 were updated to match.

## 2026-09-29T12:42:32-05:00 — Untracked files at the start of the runner work

`docs/runner/`, `AGENTS.md`, `.claude/settings.json` and
`.claude/rules/runner.md` are committed: they are the build and review
instructions. `backend/exports/` is now gitignored; it holds exported copies
of run text, the same data as `backend/data/`. `backend/export_runs.py` stays
untracked for now, because the runner does not need it.
