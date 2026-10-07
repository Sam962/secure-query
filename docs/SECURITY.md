# Security model

## Threat model

| Threat | Control |
|--------|---------|
| LLM writes SQL (injection) | Planner emits LogicalPlan JSON only; compile is AST-only |
| LLM invents columns/tables | `validate(plan, catalog)` against approved catalog |
| PII exfiltration | Column policy; high-PII blocked in filters/projections |
| Wrong confident answers | Guards + refusal; holdout wrong-rate = 0 gate |
| Over-broad queries | Required `limit`; execute timeout + row cap |
| Caller claims another identity | Identity from env / Bearer token / SSO header — never from JSON body |
| Restricted user runs a privileged metric | `catalog_for_principal` drops metrics whose tables are not visible |
| Cartesian product via CROSS JOIN | `policy.cross_join_not_allowed` + compiler refusal |
| Warehouse over-read | Databricks: Unity Catalog grants / RLS / masks on a SELECT-only identity |

## Checklist (evidence)

| Control | Implementation | Verified by |
|---------|----------------|-------------|
| LLM never outputs SQL | `planner.plan.parse_plan_json` + `_SQL_LEAK_RE` | `test_planner.py`, CI grep |
| validate before compile | `validate_and_compile()` | unit tests |
| AST-only compile | `kernel/compile.py` sqlglot nodes | CI grep (no f-string SQL) |
| Execute RO + timeout + limit | `engine/execute.py`; Postgres session `default_transaction_read_only`; timeouts cancel the query | `test_execute.py`, `test_postgres.py`, `test_databricks.py` |
| Audit trail with principal | `AuditRecord` JSONL; the question is stored as SHA-256 + length, never as text | `test_execute.py`, `test_databricks.py` |
| Approved catalog only | `sample_catalog.py` / reviewed UC draft | catalog tests |
| Tenant / role isolation | `auth.py` principal + row filters + metric slice | `test_auth.py` |
| Server-side identity | `resolve_principal()` | `test_auth.py`, `test_api.py` |
| Header identity only via a trusted proxy | `SECURE_QUERY_TRUSTED_PROXIES`; startup refuses `header` mode without it | `test_auth.py` |
| No DB error text to clients | `api/http.py` handlers: generic 503 + audit id, 502 for planner failures; `/ready` names no tenant or path | `test_api.py` |
| No CROSS JOIN | validate + compile | `test_validate.py`, `test_auth.py` |

## Identity

`POST /ask` accepts only `question` and confirm/execute flags. Principal is resolved by `SECURE_QUERY_AUTH_MODE`:

| Mode | When | How |
|------|------|-----|
| `dev` | local demo (default unless `SECURE_QUERY_ENV=production`) | `SECURE_QUERY_PRINCIPAL_ID` + optional `SECURE_QUERY_ALLOWED_TABLES` |
| `token` | API in production | `Authorization: Bearer <token>` mapped via `SECURE_QUERY_API_TOKENS` or `SECURE_QUERY_PRINCIPALS_FILE` |
| `header` | behind SSO / Databricks Apps | `X-Forwarded-User` or `X-Databricks-User` looked up in the principals file. Requires `SECURE_QUERY_TRUSTED_PROXIES` (comma-separated IPs/CIDRs of the SSO proxy): the service refuses to start without it, and the header is ignored (403) unless the request comes from an allowlisted address |

Unknown tokens/users are 401/403. A missing `allowed_tables` list in the registry is **no tables**, not all tables (except `dev` mode, where omitted env means the full demo catalog).

Ratio metrics (`numerator` / `denominator`) expand to a `LogicalPlan`, so mandatory `row_filters` are injected and validated like any plan; a row filter on a table the metric does not read fails closed. `builtin` metrics compile from a code-registered AST without a plan, so they are refused when the principal has `row_filters`. Unity Catalog RLS is the backstop on Databricks.

## Databricks

- `execute_databricks` runs compiled SQL on a SQL warehouse. The warehouse principal must be SELECT-only (USE CATALOG / USE SCHEMA / SELECT); UC still enforces grants. On timeout the running statement is cancelled.
- `/ready` checks this when `SECURE_QUERY_DATABRICKS_SCHEMA` is set: `SHOW GRANTS ON SCHEMA` for the current user, 503 on any write-capable privilege. Only direct grants are visible, not grants through groups, so it is a necessary check, not a proof.
- `draft_catalog` / `fetch_unity_catalog_draft` build an **unapproved** catalog from `information_schema`. A data owner must review PII flags and joins before that object is the planner allowlist. Unity Catalog is warehouse governance; it does not replace the Secure Query catalog.
- Local DuckDB remains the CI/demo path. Warehouse catalogs set `sql_dialect="databricks"`.

## Rules

1. `join_keys` is a strict allowlist: an empty list allows no joins. `allow_any_join=true` is dev-only. Principal slices keep only join keys between their allowed tables.
2. Retrieval is never a security control — `validate()` always uses the full authorized catalog; a retrieval miss is a refusal, not a leak.
3. Metrics are analyst-owned definitions in the catalog (`Catalog.metrics`), validated against the catalog at load. They are offered only if every table they read is in the principal's catalog. The model can pick a `metric_id`; it never supplies a definition.
4. Never accept `principal_id`, `tenant_id`, or `allowed_tables` from a request body (`extra=forbid`). `/ask/execute` takes back the reviewed `plan` or `sql` from `/ask/confirm`; it is re-validated exactly like model output (catalog, PII, joins, row filters from the server-side principal) and regenerated, never executed as sent.
5. `SECURE_QUERY_ENV=production` refuses `AUTH_MODE=dev`.

## Postgres

- `execute_postgres` uses the same timeout, row cap, and audit record as DuckDB. Each session sets `default_transaction_read_only = on`, so Postgres itself refuses writes, and `statement_timeout`, so the server cancels a slow query.
- Compile with `sql_dialect="postgres"` (set automatically when `SECURE_QUERY_BACKEND=postgres`).
- Use a SELECT-only role. Do not reuse a role that can write agent memory or DDL.

## Adversarial coverage

`tests/test_adversarial.py` checks: SQL in planner JSON, SQL pasted as the question, and unknown API fields. Compile is AST-only; CI greps `kernel/compile.py` for f-string SQL.
