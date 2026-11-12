# Handover — start here

**Updated:** 2026-11-11. Read this file, then [PLAN.md](PLAN.md) (the task list), then
[STATUS.md](STATUS.md) (what is measured).

## What this project is

Governed talk-to-data. A user asks in English; an LLM writes SQL; the kernel parses it,
checks every expression against a human-approved catalog, and **regenerates** the SQL
that runs. Model SQL never runs as written. Every answer or refusal writes a trace record
and returns a signed Evidence Bundle. The goal is to beat Databricks Genie on
adjudicated wrong-rate (PLAN.md, Goal).

## Setup (once per machine)

```bash
cd secure-query
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,planner,api]"
python -m secure_query.demo.load_chinook      # downloads the pinned demo DB to data/
```

Keep the repo outside `~/Desktop` and `~/Documents`. iCloud Drive syncs those folders and
hides files inside `.venv`, which breaks the editable install.

## The request path (where to look)

| Step | File |
|------|------|
| HTTP + UI | `src/secure_query/api/http.py`, `api/static/index.html` |
| Ask pipeline (one function, `ask`) | `src/secure_query/api/service.py` |
| Identity and per-user catalog slice | `src/secure_query/auth/__init__.py` |
| LLM writes SQL; two-readings gate | `src/secure_query/planner/sql_plan.py` |
| Allowlist validator (the security core) | `src/secure_query/kernel/sql_validate.py` |
| Execute (DuckDB / Postgres / Databricks) | `src/secure_query/engine/runtime.py` |
| Trace, fingerprints, Evidence Bundle | `src/secure_query/kernel/trace.py`, `kernel/evidence.py` |
| Eval harness and Genie scorecard | `src/secure_query/evals/accuracy.py`, `evals/scorecard.py` |

Layering: `api → engine/planner → kernel`. The kernel never imports the planner or an LLM.

## Everyday commands

```bash
export PYTHONPATH=src
pytest -q                                                   # ~7 s, no network
python -m secure_query.demo.ask --provider mock "revenue by country"
python -m secure_query.evals.run --split holdout --provider mock   # SQL path by default
python -m secure_query.evals.scorecard --suite src/secure_query/evals/suites/chinook/cases.json --sq-only --provider mock
python scripts/databricks_smoke.py                          # exits 2 (skip) without credentials
```

## Rules that bite

- Follow PLAN.md in order, one task per change. Ideas go to the PLAN parking lot.
- Sam commits, pushes and merges. Never add AI co-author trailers.
- Never tune on holdout cases. Never put dataset or domain words in code.
- Wrong-rate claims are adjudicated. A model's rating is never called "human".
