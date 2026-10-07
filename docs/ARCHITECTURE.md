# Architecture

The core install depends only on `pydantic` and `sqlglot`; DuckDB, FastAPI, the
LLM client and warehouse drivers are optional extras. `import secure_query`
loads the ask pipeline (`api.service`) but not FastAPI — import
`secure_query.api.http` for the app. For the demo, follow the root
[README](../README.md).

## Flow

```
question → LogicalPlan JSON (LLM) → validate(catalog) → AST compile → execute → audit
```

Default path: the LLM emits a LogicalPlan, never SQL. An experimental SQL
planner (ADR 003) writes SQL that `kernel.sql_validate` parses, checks, and
regenerates — the model text still never runs.

## Layers

| Package | Role | May import |
|---------|------|------------|
| `kernel/` | LogicalPlan IR, catalog, validate, joins, metrics, AST compile, explain | — |
| `planner/` | Prompt, LLM clients, NL → LogicalPlan, deterministic guards | `kernel`, `auth` |
| `engine/` | Execute backends, domain catalogs, runtime wiring, catalog draft | `kernel`, `planner`, `auth`, `demo` |
| `auth/` | Server-side principal and per-principal catalog slice | `kernel` |
| `api/` | FastAPI app, ask pipeline, templated answers, static UI | everything above |
| `evals/` | Execution-accuracy harness, golden cases, `suites/` data | everything above |
| `demo/` | Chinook / Northwind catalogs, pinned loaders, CLI | everything above |

`kernel` is the trust boundary: it never sees an LLM, and nothing outside it
writes SQL.

## Quick start

```bash
pip install -e ".[dev,planner,api]"
python -m secure_query.demo.load_chinook
ollama pull qwen2.5:7b
python -m secure_query.demo.ask --provider ollama "revenue by country"
pytest -q
```

```python
from secure_query import ask
from secure_query.auth import Principal
from secure_query.engine.runtime import runtime_config
from secure_query.demo.chinook import sample_catalog
from secure_query.planner import MockLLMClient

outcome = ask(
    "revenue by country",
    principal=Principal(principal_id="demo-user", tenant_id="chinook"),
    config=runtime_config(),
    catalog=sample_catalog(),
    client=MockLLMClient(),
    confirm_only=True,
)
print(outcome.explanation, outcome.sql)
```

Default compile dialect is DuckDB. Postgres/Databricks set `sql_dialect` from the execute backend.

This package does not depend on other internal apps. Decisions: `docs/adr/`. Status: `docs/STATUS.md`.
