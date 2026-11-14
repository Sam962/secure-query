# Handover — start here

**Updated:** 2026-11-13. Read this file, then [PLAN.md](PLAN.md) (the task list), then
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
| LLM client (any OpenAI-compatible model) | `src/secure_query/planner/llm.py` |
| LLM writes SQL; two-readings gate | `src/secure_query/planner/sql_plan.py` |
| SQL rewrites before validation (value grounding, ranking measure) | `src/secure_query/planner/sql_values.py` |
| Allowlist validator (the security core) | `src/secure_query/kernel/sql_validate.py` |
| Execute (DuckDB / Postgres / Databricks) | `src/secure_query/engine/runtime.py` |
| Trace, fingerprints, Evidence Bundle | `src/secure_query/kernel/trace.py`, `kernel/evidence.py` |
| Eval harness and Genie scorecard | `src/secure_query/evals/accuracy.py`, `evals/scorecard.py` |

Layering: `api → engine/planner → kernel`. The kernel never imports the planner or an LLM.

## Model

The default is OpenAI `gpt-6-luna` with reasoning off (`DEFAULT_MODEL` in `planner/llm.py`).
`gpt-4.1-mini`, the model behind the older numbers, is removed by OpenAI on 2027-01-14.
Any model on an OpenAI-compatible endpoint runs; the client drops a parameter the model
rejects. Measure a model before trusting it: [EVAL.md](EVAL.md), "Qualifying a new model".
Live evals need `OPENAI_API_KEY` in `.env` and cost a few dollars per Spider run.

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
- Tune on dev suites and Spider dev only. Spider test and the holdouts are for one
  confirming run, never for tuning.
- A change to what an answer shows can break the grader. After one, re-check the tie rule
  and the column flags (`extra_columns_ok`, `subset_columns_ok`) on a full run.
