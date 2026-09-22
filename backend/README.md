# MITSS backend

FastAPI application over a dependency-free core. Nothing below `app/` imports
a third-party package, and nothing below `pipeline/` touches the network.

```
app/       HTTP layer (FastAPI) — routes and request models only
  main.py    routes, CORS, error translation
  service.py business logic; knows nothing about FastAPI
pipeline/  the product: prompts, inputs, runs, verdicts, comparison
  models.py      dataclasses for prompts, versions, inputs, runs, registrations
  store.py       filesystem storage and the append-only event index
  render.py      substitutes an input into a prompt template
  compare.py     the version-by-model matrix and run-to-run diffs
  digest.py      verdicts rolled up by prompt, version and model
  transcript.py  the rolling plain-text log of every run
mitss/     the model harness, plus a worked scheduling example
  llm.py         provider interface — used by app/service.py, this is live
  model.py       dataclasses for plans and schedules       \
  textplan.py    the plain-text grammar parser              |
  validate.py    structural validation                      |
  constraints.py hard-constraint checking                   | the scheduling
  capture.py     pulls JSON out of a messy model reply      | example; the
  packet.py      builds the scheduling packets              | pipeline does
  render.py      table, CSV and ASCII timeline              | not depend on
  diffing.py     run-to-run comparison                      | any of it
  runlog.py      run storage and the append-only index      |
  cli.py         command line interface                    /
data/      prompts, inputs, runs, registrations, transcript (gitignored)
inputs/    plans written by hand for the command line
runs/      the scheduling example's own run folder (gitignored)
tests/     unit and API tests
```

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Interactive docs at http://127.0.0.1:8000/docs

## Test it

```bash
python -m unittest discover tests
```

The core suite needs nothing installed. The API tests skip themselves if
FastAPI is absent, so `mitss` can be tested on a bare interpreter.

## Configure a model

Copy `.env.example` to `.env`. Without it the harness stays in manual mode and
never calls anything. Credentials are read at call time and never stored.
