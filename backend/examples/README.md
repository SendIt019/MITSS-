# Worked example: scheduling

The `mitss/` package is a fully built domain: it parses a plain-text plan,
generates a prompt that states every rule an answer will be checked against,
and can validate a returned schedule against those rules.

It is kept here as an example, not as the product. The pipeline itself is
domain-neutral — text in, text out — and does not know what any prompt is
about.

## Use it

```bash
cd backend
python -m examples.seed_scheduling
```

That adds the generated scheduling packet to the prompt library, so you can see
what a heavily specified prompt looks like and iterate on it like any other.

## Validate a captured output against the scheduling rules

The scheduling domain ships a checker most prompts will not have. To run it on
an output you captured:

```python
from mitss.capture import extract_json
from mitss.constraints import check_constraints
from mitss.validate import validate_plan, validate_schedule
from mitss.textplan import parse_text_plan
from mitss.issues import format_issues

plan_dict, _ = parse_text_plan(open("plan.txt").read())
plan, _ = validate_plan(plan_dict)
parsed, _ = extract_json(open("output.txt").read())
schedule, issues = validate_schedule(parsed, plan)
print(format_issues(issues + check_constraints(plan, schedule)))
```

The scheduling command line also still works on its own:

```bash
python -m mitss new my-session
python -m mitss stage
python -m mitss ingest --model my-custom-model
python -m mitss report
```
