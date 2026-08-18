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
| LLM never outputs SQL | `planner.parse_plan_json` + `_SQL_LEAK_RE` | `test_planner.py`, CI grep |
| validate before compile | `validate_and_compile()` | unit tests |
| AST-only compile | `compile.py` sqlglot nodes | CI grep (no f-strings) |
| Execute RO + timeout + limit | `execute.py` | `test_execute.py` |
| Audit trail with principal | `AuditRecord` JSONL | `test_execute.py`, `test_databricks.py` |
| Approved catalog only | `sample_catalog.py` / reviewed UC draft | catalog tests |
| Tenant / role isolation | `auth.py` principal + row filters + metric slice | `test_auth.py` |
| Server-side identity | `resolve_principal()` | `test_auth.py`, `test_api.py` |
| No CROSS JOIN | validate + compile | `test_validate.py`, `test_auth.py` |

## Identity

`POST /ask` accepts only `question` and confirm/execute flags. Principal is resolved by `SECURE_QUERY_AUTH_MODE`:

| Mode | When | How |
|------|------|-----|
| `dev` | local demo (default unless `SECURE_QUERY_ENV=production`) | `SECURE_QUERY_PRINCIPAL_ID` + optional `SECURE_QUERY_ALLOWED_TABLES` |
| `token` | API in production | `Authorization: Bearer <token>` mapped via `SECURE_QUERY_API_TOKENS` or `SECURE_QUERY_PRINCIPALS_FILE` |
| `header` | behind SSO / Databricks Apps | `X-Forwarded-User` or `X-Databricks-User` looked up in the principals file |

Unknown tokens/users are 401/403. A missing `allowed_tables` list in the registry is **no tables**, not all tables (except `dev` mode, where omitted env means the full demo catalog).

Ratio metrics compile SQL without a `LogicalPlan`, so they are refused when the principal has mandatory `row_filters`. Unity Catalog RLS is the backstop on Databricks.

## Databricks

- `execute_databricks` runs compiled SQL on a SQL warehouse. Use a SELECT-only token; UC still enforces grants.
- `draft_catalog` / `fetch_unity_catalog_draft` build an **unapproved** catalog from `information_schema`. A data owner must review PII flags and joins before that object is the planner allowlist.
- Local DuckDB remains the CI/demo path. Warehouse catalogs set `sql_dialect="databricks"`.

## Rules

1. Empty `join_keys` is dev-only; production catalogs must allowlist joins.
2. Retrieval (future) is never a security control — always validate the full authorized catalog.
3. Ratio metrics compile via whitelisted AST builders in `metrics.py`, not LLM SQL, and only if every table they read is in the principal's catalog.
4. Never accept `principal_id`, `tenant_id`, or `allowed_tables` from a request body.
