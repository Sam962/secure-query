# Security model

## Threat model

| Threat | Control |
|--------|---------|
| LLM writes SQL (injection) | Model SQL is parsed, **allowlist**-checked against the catalog, and regenerated (`kernel/sql_validate.py`); its text never runs. COLUMNS/#n/STRUCT/whole-row/VERSION refuse |
| LLM invents columns/tables | Every expression must resolve to an approved catalog column |
| PII exfiltration | Column policy; high-PII blocked; shapes that skip Column nodes are forbidden |
| Wrong confident answers | Consistency gate + refusal; adjudicated wrong-rate is the product metric |
| Over-broad queries | Required `limit`; execute timeout + row cap |
| Caller claims another identity | Identity from env / Bearer token / SSO header — never from JSON body |
| Restricted user runs a privileged metric | `catalog_for_principal` drops metrics whose tables are not visible |
| Cartesian product via CROSS JOIN | `policy.cross_join_not_allowed` + compiler refusal; SQL path: `JOIN … ON` must be AND-ed equalities with an approved key (no OR / NOT) |
| Memory exhaustion by one query | SQL path refuses recursive CTEs and value generators (`repeat`, `lpad`, `generate_series`, `range`); a timeout bounds time, not memory |
| Caller runs their own SQL via `/ask/execute` | Reviews are HMAC-signed for the principal (`SECURE_QUERY_REVIEW_SECRET`); unsigned or foreign reviews get 403 |
| A leaked review is replayed later | The signature carries an expiry (`SECURE_QUERY_REVIEW_TTL_SECONDS`, default 15 minutes) |
| Audit file edited after the fact | Trace and audit lines are hash-chained (`kernel/ledger.py`); `scripts/verify_audit.py` finds an edited or removed line |
| One caller floods the model or warehouse | Per-person rate limit (`SECURE_QUERY_RATE_LIMIT_PER_MINUTE`, default 60 in production) |
| Unreviewed catalog reaches production | Startup refuses a catalog without an `approval` record or with `allow_any_join` |
| Warehouse over-read | Databricks: Unity Catalog grants / RLS / masks on a SELECT-only identity; the session is pinned to `SECURE_QUERY_DATABRICKS_SCHEMA` so unqualified names resolve in the approved schema |

## Checklist (evidence)

| Control | Implementation | Verified by |
|---------|----------------|-------------|
| LQP planner never outputs SQL | `planner.plan.parse_plan_json` + `_SQL_LEAK_RE` | `test_planner.py`, CI grep |
| Model SQL is allowlist-validated and regenerated | `kernel/sql_validate.py`: one read-only query, catalog columns only, no PII, no COLUMNS/#n/STRUCT/whole-row, approved joins (derived sides do not auto-approve), fan-out incl. CTE launder, row filters, integer LIMIT | `test_sql_validate.py`, `test_adversarial.py` |
| Every ask is traced | `kernel/trace.py` TraceRecord on ok/confirm/refuse | `test_trace.py` |
| Only server-issued reviews execute | `api/service.py` `sign_review` / `execute_reviewed` | `test_api.py` |
| validate before compile | `validate_and_compile()` | unit tests |
| AST-only compile | `kernel/compile.py` sqlglot nodes | CI grep (no f-string SQL) |
| Execute RO + timeout + limit | `engine/execute.py`; Postgres session `default_transaction_read_only`; timeouts cancel the query | `test_execute.py`, `test_postgres.py`, `test_databricks.py` |
| Audit trail with principal | `AuditRecord` JSONL; the question is stored as SHA-256 + length, never as text | `test_execute.py`, `test_databricks.py` |
| Approved catalog only | `demo/chinook.py` / reviewed UC draft | catalog tests |
| Tenant / role isolation | `auth/__init__.py` principal + row filters + metric slice | `test_auth.py` |
| Server-side identity | `resolve_principal()` | `test_auth.py`, `test_api.py` |
| Header identity only via a trusted proxy | `SECURE_QUERY_TRUSTED_PROXIES`; startup refuses `header` mode without it | `test_auth.py` |
| No DB error text to clients | `api/http.py` handlers: generic 503 + audit id, 502 for planner failures; `/ready` names no tenant or path | `test_api.py` |
| Value lookups (SQL planner) stay inside policy | `planner/sql_values.py`: lookups pass through `validate_sql` with the principal's row filters, run on the read-only executor and are audited; stored values are never sent to the LLM, only whether a literal the model wrote exists and which other column of that table stores it; a `no_stored_value` refusal shows the person asking up to three close stored values, read under their own permissions | `test_sql_values.py` |
| No CROSS JOIN | validate + compile | `test_validate.py`, `test_auth.py` |

## Identity

`POST /ask` accepts only `question` and confirm/execute flags. Principal is resolved by `SECURE_QUERY_AUTH_MODE`:

| Mode | When | How |
|------|------|-----|
| `dev` | local demo (default unless `SECURE_QUERY_ENV=production`) | `SECURE_QUERY_PRINCIPAL_ID` + optional `SECURE_QUERY_ALLOWED_TABLES` |
| `token` | API in production | `Authorization: Bearer <token>` mapped via `SECURE_QUERY_API_TOKENS` or `SECURE_QUERY_PRINCIPALS_FILE` |
| `jwt` | OIDC identity provider (Entra ID, Okta, …) | `Authorization: Bearer <JWT>` verified against the provider's keys: signature (asymmetric algorithms only), issuer, audience, expiry. Needs `SECURE_QUERY_OIDC_ISSUER` and `SECURE_QUERY_OIDC_AUDIENCE`; `SECURE_QUERY_OIDC_JWKS_URL` skips discovery. A subject listed under `principals` uses that entry; otherwise the token's groups claim is matched against `groups` in the principals file. Tables are the union of the matched groups; every matched group's row filters apply |
| `header` | behind SSO / Databricks Apps | `X-Forwarded-User` or `X-Databricks-User` looked up in the principals file. Requires `SECURE_QUERY_TRUSTED_PROXIES` (comma-separated IPs/CIDRs of the SSO proxy): the service refuses to start without it, and the header is ignored (403) unless the request comes from an allowlisted address |

Unknown tokens/users are 401/403. A missing `allowed_tables` list in the registry is **no tables**, not all tables (except `dev` mode, where omitted env means the full demo catalog).

Ratio metrics (`numerator` / `denominator`) expand to a `LogicalPlan`, so mandatory `row_filters` are injected and validated like any plan; a row filter on a table the metric does not read fails closed. `builtin` metrics compile from a code-registered AST without a plan, so they are refused when the principal has `row_filters`. Unity Catalog RLS is the backstop on Databricks.

The principals file is re-read when it changes, so access can be changed without a restart.

## What is sent to the model provider

| Sent | Not sent |
|------|----------|
| The question, as typed | Rows, query results, or any stored value the catalog does not list |
| Names, types and descriptions of the tables and columns this person may see; a column's `values` list where the catalog owner approved one (category codes, never a PII column); approved join keys; synonyms; metric ids; house rules | Who is asking (no principal, tenant or token) |
| On a repair: the validator's error messages; the database's error for the model's own query (first line, quoted values replaced by '…'); a value from the model's own SQL that matched nothing stored, and the name of another column of that table that stores it | Tables and columns outside this person's catalog slice |

Table and column names can themselves be sensitive. To keep them inside your network, point
`SECURE_QUERY_BASE_URL` at a private OpenAI-compatible endpoint (Azure OpenAI, Databricks
model serving, a self-hosted model) and qualify that model first ([EVAL.md](EVAL.md)).

## Signing and audit

- **Reviews** are HMAC-signed with `SECURE_QUERY_REVIEW_SECRET` and expire. To rotate, set the
  new secret and move the old one to `SECURE_QUERY_REVIEW_SECRET_PREVIOUS`; it verifies but
  no longer signs. Remove it after the review lifetime has passed.
- **Evidence Bundles** are signed with Ed25519 when `SECURE_QUERY_EVIDENCE_PRIVATE_KEY` (PEM
  text or a file path) is set. `GET /evidence/public-key` serves the public key and its id,
  so an auditor verifies a bundle without any secret. Without the key, bundles use the HMAC
  secret and only this service can verify them.
- **Trace and audit files** are hash-chained (an HMAC chain when the secret is set). Run
  `python scripts/verify_audit.py data/trace.jsonl`. A chain cannot show that the newest
  lines were cut off, so also ship the lines off the host: `SECURE_QUERY_AUDIT_STDOUT=1`
  prints each one to stdout for your log collector.
- **Limits of the built-in controls:** the rate limit and the metrics are per process. With
  several workers, put the exact limit at your gateway.

## Databricks

- `execute_databricks` runs compiled SQL on a SQL warehouse as the **end-user** identity by default (`SECURE_QUERY_DATABRICKS_AUTH=user`): the caller’s token comes from `X-Databricks-Access-Token` / principal `databricks_token` / OAuth on-behalf-of. `shared` mode (service PAT) is demo-only. UC RLS/masks apply to the person asking. On timeout the running statement is cancelled.
- The warehouse principal must be SELECT-only (USE CATALOG / USE SCHEMA / SELECT). `/ready` checks this when `SECURE_QUERY_DATABRICKS_SCHEMA` is set: `SHOW GRANTS ON SCHEMA` for the current user, 503 on any write-capable privilege. Only direct grants are visible, not grants through groups, so it is a necessary check, not a proof.
- `draft_catalog` / `fetch_unity_catalog_draft` build an **unapproved** catalog from `information_schema`. Foreign keys come from `referential_constraints` + key/constraint column usage — query failure fails the draft (no silent empty join allowlist). Untagged columns whose names look sensitive (`email`, `phone`, `address`, …) default to `pii=high`; explicit `public` / `pii_none` clears that. A data owner must still review before the object is the planner allowlist.
- Postgres and SharePoint lists join UC via Lakehouse Federation / Lakeflow Connect → Delta ([FEDERATION.md](FEDERATION.md)); they do not become a second production dialect.
- Local DuckDB remains the CI/demo path. Warehouse catalogs set `sql_dialect="databricks"`.
- Every ask writes a TraceRecord and a signed Evidence Bundle (see Signing and audit).
- Warehouse sessions are reused only for the same host, warehouse, schema and access token, and dropped after any failed statement.

## Rules

1. `join_keys` is a strict allowlist: an empty list allows no joins. `allow_any_join=true` is dev-only. Principal slices keep only join keys between their allowed tables.
2. Retrieval is never a security control — `validate()` always uses the full authorized catalog; a retrieval miss is a refusal, not a leak.
3. Metrics are analyst-owned definitions in the catalog (`Catalog.metrics`), validated against the catalog at load. They are offered only if every table they read is in the principal's catalog. The model can pick a `metric_id`; it never supplies a definition.
4. Never accept `principal_id`, `tenant_id`, or `allowed_tables` from a request body (`extra=forbid`). `/ask/execute` takes back the reviewed `plan` or `sql` from `/ask/confirm`; it is re-validated exactly like model output (catalog, PII, joins, row filters from the server-side principal) and regenerated, never executed as sent.
5. `SECURE_QUERY_ENV=production` refuses `AUTH_MODE=dev`, refuses a catalog without an `approval` record, and refuses to start without a `SECURE_QUERY_REVIEW_SECRET` of at least 32 characters (Evidence Bundles must verify across restarts and workers).

## Postgres

- `execute_postgres` uses the same timeout, row cap, and audit record as DuckDB. Each session sets `default_transaction_read_only = on`, so Postgres itself refuses writes, and `statement_timeout`, so the server cancels a slow query.
- Compile with `sql_dialect="postgres"` (set automatically when `SECURE_QUERY_BACKEND=postgres`).
- Use a SELECT-only role. Do not reuse a role that can write agent memory or DDL.

## Adversarial coverage

`tests/test_adversarial.py` checks: SQL in planner JSON, SQL pasted as the question, and unknown API fields. Compile is AST-only; CI greps `compile.py`, `sql_validate.py`, and the execute backends for f-string SQL.
