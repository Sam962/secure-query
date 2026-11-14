# Architecture

The core install depends only on `pydantic` and `sqlglot`; DuckDB, FastAPI, the
LLM client and warehouse drivers are optional extras. `import secure_query`
loads the ask pipeline (`api.service`) but not FastAPI — import
`secure_query.api.http` for the app. For the demo, follow the root
[README](../README.md).

## Flow

```
question → SQL JSON (LLM, twice) → allowlist validate(catalog) → regenerate AST → execute → trace/audit + signed evidence
```

Default path (ADR 005): the LLM emits SQL; `kernel.sql_validate` parses it,
allowlists every expression against the approved catalog, and regenerates the
SQL that runs. LQP remains for catalog-owned metrics.

## Layers

| Package | Role | May import |
|---------|------|------------|
| `kernel/` | Catalog, SQL allowlist validator, LogicalPlan IR, joins, metrics, AST compile, explain, trace, evidence | — |
| `planner/` | LLM clients, validated-SQL planner (default), LogicalPlan planner (metrics), value grounding, consistency checks | `kernel`, `auth` |
| `engine/` | Execute backends, domain catalogs, runtime wiring, catalog draft | `kernel`, `planner`, `auth`, `demo` |
| `auth/` | Server-side principal and per-principal catalog slice | `kernel` |
| `api/` | FastAPI app, ask pipeline, templated answers, static UI | everything above |
| `evals/` | Execution-accuracy harness, Genie scorecard, golden cases, `suites/` data | everything above |
| `demo/` | Chinook / Northwind catalogs, pinned loaders, CLI | everything above |

`kernel` is the trust boundary: it never sees an LLM. Model SQL reaches the
warehouse only after allowlist validation and regeneration inside the kernel.

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
