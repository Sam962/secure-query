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
| `planner.py` | Phase 2: question → LogicalPlan JSON only |
| `examples/sample_catalog.py` | **Active** Chinook allowlist |
| `explain.py` | LogicalPlan → plain English, for human confirmation |
| `guard.py` | Deterministic question guards (no model involved) |
| `examples/load_sample_db.py` | Chinook → DuckDB |
| `examples/run_sample_query.py` | E2E local execute |
| `examples/ask_sample.py` | E2E NL → plan → SQL → results |
| `examples/demo_catalog.py` | Re-exports sample catalog |
| `tests/` | Compiler + validator + IR + planner tests |

## Quick start

```bash
# from repo root (or after copying package next to your app)
pip install -e ".[dev]"
python -m secure_query.examples.load_sample_db
python -m secure_query.examples.demo_catalog
python -m secure_query.examples.run_sample_query
python -m secure_query.examples.ask_sample "revenue by country"
pytest -q
```

```python
from secure_query import plan_question, default_client
from secure_query.examples.sample_catalog import sample_catalog

result = plan_question(
    "revenue by country",
    sample_catalog(),
    default_client(),  # MockLLMClient if no OPENAI_API_KEY
)
assert result.status == "ok"
print(result.compiled.sql)  # compiled locally — not from the LLM
```

## Practical plan (do in order)

Full feature plan: see repo root **`docs/PLAN.md`**.

Phase 2 entrypoint: `secure_query.planner.plan_question` + `examples/ask_sample.py`.

### Phase 0 — Land the kernel (done here)

- [x] IR + builder + AST compiler
- [x] Catalog model + validate gate
- [x] `validate_and_compile`
- [ ] Copy `secure_query/` into your other repo; run tests there

### Phase 1 — Catalog

1. Chinook `sample_catalog.py` + DuckDB (`load_sample_db` / `run_sample_query`).
2. Mark PII columns (`pii_risk="high"`).
3. Treat the catalog as eng/business-approved, not LLM-authored.

### Phase 2 — LLM planner only emits plans

1. Send `catalog.planner_summary()` to the model (names + descriptions — not row data).
2. Force structured output to `LogicalPlan` JSON Schema (`LogicalPlan.model_json_schema()`).
3. On `PlanValidationFailed`, optionally one repair loop with error codes; then stop/clarify.
4. Never ask the model for SQL; never paste SQL into the prompt for “fixes.”

### Phase 3 — Execute + policy

1. Run SQL with a read-only DB role + statement timeout.
2. Enforce tenant / RLS in **your** execute layer (not in the LLM).
3. Cap rows. `validate_and_compile` passes an explicit catalog projection, so list
   intents emit named columns and drop `pii_risk="high"` ones. Calling bare
   `compile(plan)` without a projection still emits `*` — don't do that in product code.
4. Decide: may a summarizer LLM see result cells? If no → templates/charts only.

### Phase 4 — Extreme accuracy

1. Golden tests: fixture `plan.json` → exact `expected.sql`.
2. Execution accuracy: 32 questions with reference SQL (`evals/chinook_live.json`),
   run with `python -m secure_query.evals.run_chinook --accuracy --provider ollama`.
   Latest on the full 11-table catalog: 94% correct, 0% wrong, 6% declined.
3. Refuse rather than answer a near-miss — see `guard.py` and the `policy.*`
   validation codes. A declined question is a bug to fix; a wrong answer is a breach.
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
