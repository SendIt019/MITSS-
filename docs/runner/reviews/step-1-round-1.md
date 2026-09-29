# Step 1 — round 1

Codex's Step 1 review, as Jake pasted it, with his decisions. The Codex text
itself was not included in the paste, so this file holds Jake's summary of
each finding and his decision, verbatim.

> Codex's review of Step 1 is below, with my decisions. Fix these, rerun the gates, log the decisions, then stop and report. Don't start Step 2.
>
> Credential read: downgrade to minor, but fix it. Read registrations through the store (the registration's summary), not service.get_model / _model_payload, so the runner never touches the key environment variable, even to test whether it exists. Add a test.
> Remote endpoints: agreed. plan and check refuse any registration whose URL isn't loopback (127.0.0.1, localhost, ::1), with tests.
> Store writes: agreed. check and plan must not create anything under data/. Use read-only access and add a test against a missing data directory.
> Ruff: pin the gate to uvx ruff check --target-version py39 <changed files>, log that as a decision (Python 3.9 compatibility beats ruff's 3.10 syntax advice), and fix any findings that remain under that setting. The gate must pass, not be waived.
