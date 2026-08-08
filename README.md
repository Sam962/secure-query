# Secure Query

Standalone trust kernel for talk-to-data: **LLM never writes SQL**.

```
user question
  → LLM emits LogicalPlan JSON only (structured output)
  → validate(plan, approved Catalog)
  → compile(plan) via sqlglot AST
  → execute with RO role, timeout, limit
  → audit: plan + sql + hashes
```

## Install

```bash
cd secure-query
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

```bash
python -m secure_query.examples.demo_catalog
pytest -q
```

```python
from secure_query import validate_and_compile
from secure_query.builder import LQP
from secure_query.examples.demo_catalog import demo_catalog

catalog = demo_catalog()
plan = (
    LQP.aggregate(table="orders")
    .join("customers", on=[("orders.customer_id", "customers.customer_id")])
    .group_by_columns(["customers.region"])
    .agg("sum", "orders.amount", alias="total_amount")
    .limit(10)
    .build()
)
compiled = validate_and_compile(plan, catalog)
print(compiled.sql)
```

## Layout

| Path | Role |
|------|------|
| `secure_query/logical_plan.py` | Frozen Pydantic IR |
| `secure_query/builder.py` | Fluent helpers for tests/fixtures |
| `secure_query/catalog.py` | Approved table/column/join allowlist |
| `secure_query/validate.py` | Catalog typecheck + policy gate |
| `secure_query/compile.py` | LogicalPlan → SQL (AST only; default DuckDB) |
| `secure_query/examples/demo_catalog.py` | Runnable demo — replace with your domain |
| `secure_query/tests/` | Compiler + validator + IR tests |

## Practical plan

### Phase 1 — Your catalog (accuracy + security)

1. Replace `examples/demo_catalog.py` with your tables/columns.
2. Populate `join_keys` (empty = any join between known cols — dev only).
3. Mark PII columns (`pii_risk="high"`).
4. Treat catalog as eng/business-approved, not LLM-authored.

### Phase 2 — LLM planner only emits plans

1. Send `catalog.planner_summary()` to the model (not row data).
2. Force structured output to `LogicalPlan` JSON Schema.
3. On `PlanValidationFailed`, optional one repair loop; then stop/clarify.
4. Never ask the model for SQL.

### Phase 3 — Execute + policy

1. Read-only DB role + statement timeout.
2. Tenant / RLS in your execute layer.
3. Cap rows; prefer forbidding `SELECT *` in a product fork.
4. Decide if a summarizer LLM may see result cells.

### Phase 4 — Extreme accuracy

1. Golden tests: `plan.json` → exact `expected.sql`.
2. Eval set: real questions → expected plans.
3. Clarify when linking is ambiguous.
4. Approved metrics later (“revenue” = `sum(orders.amount)`).

## Security checklist

- [ ] LLM never outputs SQL strings
- [ ] Every plan passes `validate(plan, catalog)` before compile
- [ ] Compile is AST-only (no f-string SQL)
- [ ] Execute: RO role, timeout, limit
- [ ] Tenant isolation at execute
- [ ] Audit: question, plan JSON, sql, hashes

## Notes

- Default dialect is DuckDB (`DEFAULT_DIALECT` in `compile.py`). Change for Postgres/etc.
- Extracted from taf-talk LQP work; this repo is independent.
