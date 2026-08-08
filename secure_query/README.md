# Secure Query — extractable trust kernel
#
# Copy this entire `secure_query/` folder into your other project.
# Depends on: pydantic>=2.6, sqlglot>=23.0 (duckdb optional, for tests).

## Goal

LLM never writes SQL. Flow:

```
user question
  → LLM emits LogicalPlan JSON only (structured output)
  → validate(plan, approved Catalog)     # reject bad refs / types / policy
  → compile(plan) via sqlglot AST        # deterministic SQL + hashes
  → execute with RO role, timeout, limit
  → audit: plan + sql + plan_hash + sql_hash
```

## Package layout

| File | Role |
|------|------|
| `logical_plan.py` | Frozen Pydantic IR (filters, joins, aggs, …) |
| `builder.py` | Fluent helpers for tests/fixtures |
| `catalog.py` | **Approved** table/column/join allowlist |
| `validate.py` | Catalog typecheck + policy gate |
| `compile.py` | LogicalPlan → DuckDB SQL (AST only) |
| `errors.py` | Typed ValidationError |
| `examples/demo_catalog.py` | Runnable demo |
| `tests/` | Compiler + validator + IR tests |

## Quick start

```bash
# from repo root (or after copying package next to your app)
PYTHONPATH=. python -m secure_query.examples.demo_catalog

PYTHONPATH=. pytest secure_query/tests -q
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

## Practical plan (do in order)

### Phase 0 — Land the kernel (done here)

- [x] IR + builder + AST compiler
- [x] Catalog model + validate gate
- [x] `validate_and_compile`
- [ ] Copy `secure_query/` into your other repo; run tests there

### Phase 1 — Your catalog (accuracy + security)

1. Replace `examples/demo_catalog.py` with **your** tables/columns.
2. Populate `join_keys` (empty list = any join between known cols — fine for dev only).
3. Mark PII columns (`pii_risk="high"`).
4. Treat catalog as eng/business-approved, not LLM-authored.

### Phase 2 — LLM planner only emits plans

1. Send `catalog.planner_summary()` to the model (names + descriptions — not row data).
2. Force structured output to `LogicalPlan` JSON Schema (`LogicalPlan.model_json_schema()`).
3. On `PlanValidationFailed`, optionally one repair loop with error codes; then stop/clarify.
4. Never ask the model for SQL; never paste SQL into the prompt for “fixes.”

### Phase 3 — Execute + policy

1. Run SQL with a read-only DB role + statement timeout.
2. Enforce tenant / RLS in **your** execute layer (not in the LLM).
3. Cap rows; prefer forbidding `SELECT *` in a product fork (compiler still emits `*` for list intents).
4. Decide: may a summarizer LLM see result cells? If no → templates/charts only.

### Phase 4 — Extreme accuracy

1. Golden tests: fixture `plan.json` → exact `expected.sql`.
2. Eval set: real questions → expected plans (not just SQL).
3. Clarify when schema linking is ambiguous.
4. Add approved metrics later (“revenue” = `sum(orders.amount)`).

## Security checklist

- [ ] LLM never outputs SQL strings
- [ ] Every plan passes `validate(plan, catalog)` before compile
- [ ] Compile is AST-only (no f-string SQL) — already true in `compile.py`
- [ ] Execute: RO role, timeout, limit
- [ ] Tenant isolation at execute
- [ ] Audit store: question, plan JSON, sql, hashes

## Porting notes

- Default dialect is DuckDB (`compile.py` `DEFAULT_DIALECT`). Change to `"postgres"` etc. if needed.
- This package is self-contained; it does **not** depend on Engine 1/4, FAA fixtures, or the rest of taf-talk.
- Original ADR ideas live in `docs/adr/0001-lqp-foundation.md` in the parent repo (optional reading).

## What was intentionally left behind

Empty chat/KPI engines, FAA fixtures, unfinished audit/executor tickets, and LLM schema-enrichment pipelines. Those are product layers — rebuild them for your domain if needed.
