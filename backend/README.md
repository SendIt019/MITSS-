# MITSS backend

FastAPI application over the dependency-free `mitss` core.

```
app/       HTTP layer (FastAPI) — routes and request models only
  main.py    routes, CORS, error translation
  service.py business logic; knows nothing about FastAPI
mitss/     the core: no third-party dependencies, no network access
  model.py       dataclasses for plans and schedules
  textplan.py    the plain-text grammar parser
  validate.py    structural validation
  constraints.py hard-constraint checking
  capture.py     pulls JSON out of a messy model reply
  packet.py      builds the scheduling and structuring packets
  llm.py         provider interface for a custom model
  render.py      table, CSV and ASCII timeline
  diffing.py     run-to-run comparison
  runlog.py      run storage and the append-only index
  cli.py         command line interface
inputs/    plans written by hand for the command line
runs/      one folder per run (gitignored)
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
