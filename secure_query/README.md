# Secure Query package

Copy `secure_query/` only if you are embedding the kernel. For the product demo, stay at the **repo root** and follow the root [README](../README.md).

Depends on: `pydantic>=2.6`, `sqlglot>=23.0` (DuckDB/FastAPI optional extras).

## Flow

```
question → LogicalPlan JSON (LLM) → validate(catalog) → AST compile → execute → audit
```

The LLM never emits SQL.

## Layout

| Path | Role |
|------|------|
| `kernel/` | IR, catalog, validate, compile, explain |
| `planner/` | NL → LogicalPlan |
| `engine/` | DuckDB / Postgres / Databricks + catalog draft |
| `auth/` | Server-side identity |
| `api/` | FastAPI + ask pipeline |
| `examples/` | Chinook demo + CLI |
| `evals/` | Goldens + accuracy |
| `../frontend/` | Confirm-first UI |

## Quick start

```bash
pip install -e ".[dev,planner,api]"
python -m secure_query.examples.load_sample_db
ollama pull qwen2.5:7b
python -m secure_query.examples.ask_sample --provider ollama "revenue by country"
pytest -q
```

```python
from secure_query import ask
from secure_query.auth import Principal
from secure_query.engine.runtime import runtime_config
from secure_query.examples.sample_catalog import sample_catalog
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
