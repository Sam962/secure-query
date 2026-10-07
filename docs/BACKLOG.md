# Review backlog — 2026-10-06

Work items from the independent review. Detail and evidence live in
[`REVIEW-2026-10-06.md`](REVIEW-2026-10-06.md). Do not retune prompts or guards on
holdout case ids ([EVAL.md](EVAL.md)).

**Status:** Waves 1–4 done (2026-10-07); Waves 5–6 open. Check a box when merged; leave the ID stable.

| Wave | Theme | Items | Why this order |
|------|--------|-------|----------------|
| 1 | Product trust + correctness | C1, H1, H2, H3, H6 | Restores confirm-then-run; stops rejecting / silently wrong plans |
| 2 | Request plumbing + cost | H4, H5, M5, M6 | Latency, token ceiling, fewer repair calls |
| 3 | Security ops | H7, M1, M2, M3, L13 | Defense in depth around execute + identity |
| 4 | Reliability / SQL path | M4, M8, M9, M10, M11, M12 | Races, dialect, CI, import graph |
| 5 | Simplify | S1–S14 | Delete / merge / replace after the above land |
| 6 | Hygiene | L1–L12 | Cheap leftover cleanup |

---

## Wave 1 — product trust + correctness

### [x] C1. Pin confirm → run to the reviewed plan

- **Severity:** Critical
- **Files:** `src/secure_query/api/static/index.html` (548, 559),
  `src/secure_query/api/http.py` (170–176), `src/secure_query/api/service.py`
  (146–163, 241–251)
- **Do:** Add `POST /ask/execute` taking `{plan_hash, plan?, sql?}`. Re-validate
  without an LLM call. 409 if `compiled.plan_hash != plan_hash`. UI stores the
  confirm payload and posts it back. Keep `/ask` for one-shot execute if needed;
  stop using it as "Run" after Review.
- **Done when:** Review then Run executes the reviewed SQL even if a second
  planner call would produce different SQL. Test: confirm once, stub the LLM to
  return a different plan, execute still runs the first hash.

### [x] H1. Allow integer literals on float columns

- **Severity:** High
- **Files:** `src/secure_query/kernel/validate.py` (`_LITERAL_TO_DTYPE`),
  `src/secure_query/kernel/logical_plan.py` (`LiteralValue`)
- **Do:** `_LITERAL_TO_DTYPE["integer"]` includes `"float"`. Coerce `int → float`
  when `type == "float"` so JSON `5` is not a Pydantic reject.
- **Done when:** probes `Total > 5` with `{type:integer,value:5}` and
  `{type:float,value:5}` both validate. Add those as unit tests.

### [x] H2. Trend plans can order by their time bucket

- **Severity:** High
- **Files:** `src/secure_query/kernel/validate.py` (`_check_dead_order_keys`, ~194),
  `src/secure_query/kernel/compile.py`, `src/secure_query/planner/prompt.py`
- **Do:** Treat `tb.column` for each `group_by.time_buckets` as a grouped order
  key. Compile `ORDER BY` to the bucket expression.
- **Done when:** monthly-revenue plan ordered by `Invoice.InvoiceDate` validates
  and compiles to `ORDER BY` the month bucket.

### [x] H3. Child-table filter on a list plan must not duplicate parent rows

- **Severity:** High
- **Files:** `src/secure_query/kernel/joins.py`, `src/secure_query/kernel/validate.py`,
  `src/secure_query/kernel/compile.py`, `src/secure_query/kernel/sql_validate.py`
  (`_exists_through`, `_inject_row_filters`)
- **Do:** For plans with no aggregations, when a joined table is on the many side
  of `source`, compile the child filter as `EXISTS` (reuse `_exists_through`) and
  project only source columns. Do not `SELECT` every joined column by default.
- **Done when:** `source=Customer`, filter `Invoice.BillingCountry='USA'` returns
  13 distinct customers, not 91 invoice-grain rows. Add the Chinook probe as a test.

### [x] H6. Fan-out check must not skip scopes with a derived join side

- **Severity:** High
- **Files:** `src/secure_query/kernel/sql_validate.py` (`_check_fan_out` ~369–382,
  derived-side `approved = True` ~309)
- **Do:** Record `scope → (base tables, group keys)` from already-validated CTE
  scopes and run the grain check against that. Or refuse aggregates over joins
  with derived sides unless `allow_any_join`.
- **Done when:**
  `WITH i AS (SELECT * FROM Invoice) SELECT … SUM(…) FROM Customer JOIN i`
  is rejected (or grain-checked correctly). Test covers the CTE bypass.

---

## Wave 2 — request plumbing + cost

### [x] H4. Build catalog, registry, and LLM client once

- **Severity:** High
- **Files:** `src/secure_query/api/http.py` (99, 108, 135),
  `src/secure_query/engine/runtime.py` (70), `src/secure_query/auth/__init__.py`
  (113), `src/secure_query/api/service.py` (153, 162),
  `src/secure_query/planner/llm.py` (254, 268, 307)
- **Do:** `lru_cache(maxsize=1)` (or app-startup) for `runtime_config`,
  `default_client`, `load_principal_registry`. Expose `cache_clear()` for tests.
  Do not probe Ollama per request when `SECURE_QUERY_PROVIDER` is set.
- **Done when:** two `/ask` calls share one OpenAI client and one catalog object.
  Tests still isolate via `cache_clear()`.

### [x] H5. Harden the OpenAI client (timeout, max_tokens, no stacked retries)

- **Severity:** High
- **Files:** `src/secure_query/planner/llm.py` (12–13, 53–109, 346)
- **Do:** `OpenAI(..., timeout=30.0, max_retries=2)`. Set `max_tokens` (e.g. 1200)
  and a `seed`. Drop the 5× `time.sleep(15)` RateLimit loop and the blanket
  `except Exception` `response_format` fallback when `provider == "openai"`.
- **Done when:** a 400/auth error is not retried; a hung call fails within ~30s
  plus SDK retries; completions cannot run unbounded.

### [x] M5. Stable catalog prefix for prompt caching

- **Severity:** Medium
- **Files:** `src/secure_query/planner/prompt.py`, `src/secure_query/planner/retrieve.py`
- **Do:** Always emit the full catalog as the stable block; append
  "likely relevant tables: …" after it. Truncate the catalog only when it exceeds
  a token budget. Trim `SYSTEM_PROMPT` once M6 carries the JSON shape.
- **Done when:** two questions against the same catalog share an identical prefix
  through the catalog text.

### [x] M6. Structured outputs from `LogicalPlan`

- **Severity:** Medium
- **Files:** `src/secure_query/planner/llm.py` (79),
  `src/secure_query/kernel/logical_plan.py` (`LiteralValue.value` union),
  `src/secure_query/planner/plan.py` (JSON leak / `joins` pop)
- **Do:** `response_format` json_schema / `parse(response_format=LogicalPlan)`.
  Strict mode needs `LiteralValue.value` as a string coerced by `type`.
- **Done when:** LQP path no longer depends on `json_object` + shape prose for
  required fields. Repair calls for `JSONDecodeError` / missing fields drop.
- **Closed on gpt-4.1-mini (2026-10-06):** `SYSTEM_PROMPT` teaches the
  `{kind, plan, metric_id, limit, reason}` wrapper; hand-written JSON shapes and
  Booking/Hotel examples removed (~6.4k → 4.0k chars). Chinook dev 126/3 → 125/3
  (correct/wrong), Spider dev 200 88/25 → 92/28: neutral within noise. Repair
  calls were already 0/60 before the change on this model, so no drop is observable.
- **Open, not a Wave 2 blocker:** clients without enforced structured outputs
  (Ollama, Groq, mock) now get the response schema embedded as JSON text in the
  system prompt instead of the old shape prose. That path is unmeasured.

---

## Wave 3 — security ops

### [x] H7. Enforce read-only on Postgres and Databricks

- **Severity:** High
- **Files:** `src/secure_query/engine/postgres.py` (138–140),
  `src/secure_query/engine/databricks.py` (234)
- **Do:** After Postgres connect: `SET default_transaction_read_only = on`.
  Databricks: document SELECT-only warehouse principal; check grants at `/ready`.
- **Done when:** a compiled `SELECT` still runs; a write (if it reached execute)
  fails at the session. `/ready` reports the Databricks grant check.

### [x] M1. Stop writing raw questions to the audit log

- **Severity:** Medium
- **Files:** `src/secure_query/engine/execute.py` (`AuditRecord`, ~39–66)
- **Do:** Store `sha256(question)` + length. Keep `plan_hash` and SQL.
- **Done when:** JSONL rows have no plaintext question. Tests assert the hash field.

### [x] M2. Do not leak DB errors, unhandled PlannerError, or tenant on `/ready`

- **Severity:** Medium
- **Files:** `src/secure_query/api/http.py` (103–115, 154–155)
- **Do:** Generic 503 + audit id to the client; log the real error. Handle
  `PlannerError` → 502. Strip `catalog_tenant` from unauthenticated `/ready`
  or require auth.
- **Done when:** a forced execute error returns a generic body; `/ready` without
  auth does not name the tenant.

### [x] M3. Timeouts must cancel the in-flight query

- **Severity:** Medium
- **Files:** `src/secure_query/engine/execute.py` (172–181),
  `src/secure_query/engine/postgres.py` (141–152),
  `src/secure_query/engine/databricks.py` (257–297)
- **Do:** DuckDB: `con.interrupt()` before close. Postgres: rely on
  `statement_timeout` and drop the thread pool. Same pattern for Databricks if
  the client supports cancel.
- **Done when:** a timeout test shows the worker is not left running; no
  `shutdown(wait=False)` leak.

### [x] L13. Fail closed if `header` auth has no trusted-proxy check

- **Severity:** Low (security-adjacent)
- **Files:** `src/secure_query/auth/__init__.py` (131–135)
- **Do:** Require `SECURE_QUERY_TRUSTED_PROXIES` (or equivalent) when
  `auth_mode() == "header"`; refuse to start otherwise. Document in
  `docs/SECURITY.md`.
- **Done when:** `SECURE_QUERY_AUTH_MODE=header` without the allowlist raises at
  startup.

---

## Wave 4 — reliability / SQL path

### [x] M4. Lock or replace `_COMPILE_CACHE`

- **Severity:** Medium
- **Files:** `src/secure_query/kernel/compile.py` (43–99)
- **Do:** `threading.Lock` around the `OrderedDict`, or
  `functools.lru_cache` on a pure `(plan_json, catalog_hash, dialect)` function.
- **Done when:** concurrent compile from two threads does not raise
  `KeyError` on `move_to_end` / `popitem`.

### [x] M8. Prompt and parse SQL in `catalog.sql_dialect`

- **Severity:** Medium
- **Files:** `src/secure_query/planner/sql_plan.py` (28),
  `src/secure_query/kernel/sql_validate.py` (51, 83)
- **Do:** Stop hard-coding DuckDB in the SQL system prompt and parser. Use
  `catalog.sql_dialect` for both. Revisit `_SAFE_ANONYMOUS`
  (`strftime` / `date_part`) per dialect.
- **Done when:** a Postgres-dialect catalog is prompted and parsed as postgres.

### [x] M9. `MockLLMClient` must serve the SQL path

- **Severity:** Medium
- **Files:** `src/secure_query/planner/llm.py` (154),
  `src/secure_query/planner/sql_plan.py` (53)
- **Do:** Accept both `"User question:\n"` and `"Question:\n"` (or one shared
  marker). CI mock holdout must exercise `SECURE_QUERY_PLANNER=sql`.
- **Done when:** mock SQL-path holdout runs in CI, not only LQP.

### [x] M10. Limit must apply to unbounded CTEs

- **Severity:** Medium
- **Files:** `src/secure_query/kernel/sql_validate.py` (`_enforce_limit`, 433)
- **Do:** Push a row cap into CTE/derived scans or refuse unbounded
  `SELECT *` CTEs over catalog tables.
- **Done when:** `WITH i AS (SELECT * FROM Invoice) SELECT * FROM i LIMIT 10`
  does not materialize the full table on the warehouse path. Test the rewrite.

### [x] M11. Fix the stale editable install; make CI catch it

- **Severity:** Medium
- **Files:** `.venv` (local), `pyproject.toml`, CI workflow, Makefile if added
- **Do:** `pip install -e '.[dev,planner,api]'`. Add
  `python -c 'import secure_query'` (and `pip show` version matches
  `pyproject.toml`) to `make check` / CI.
- **Done when:** a clean `source .venv/bin/activate && pytest` collects without
  `PYTHONPATH=src`.

### [x] M12. `import secure_query` must not import FastAPI

- **Severity:** Medium
- **Files:** `src/secure_query/__init__.py`, `src/secure_query/api/__init__.py`
- **Do:** Remove the re-export or lazy-import. Align
  `docs/ARCHITECTURE.md` with the real graph.
- **Done when:** `python -c "import secure_query, sys; assert 'fastapi' not in sys.modules"`.

---

## Wave 5 — simplify

Do after Waves 1–4 so deletions do not fight correctness fixes.

### [ ] S1. Delete `kernel/builder.py`

Used only in tests/demo. `aggregate` / `rank` / `compare` / `filter_and_list`
return the same builder; `compare` ignores `right`. Tests construct `LogicalPlan`
dicts instead.

### [ ] S2. Delete Chinook presentation from the service layer

`planner/suggest.py`, `api/respond.py` `money_scale_note`, hardcoded
`{"country","name","title"}` in `result_column_units` (also L9).

### [ ] S3. Move `evals/consistency.py` and `evals/adjudicate.py` out of the package

They construct `OpenAI()` directly, bypassing `OpenAIClient`. Put them in
`scripts/` or a separate eval repo.

### [ ] S4. Drop guards with negative net value on the current model

Per STATUS / review: `dropped_concepts`, `opaque_grouping_keys` on gpt-4.1-mini
SQL. Keep `restricted_request` and `dropped_literals`. Gate the rest behind
`blocked_wrong > blocked_right`. Dedup `_STOP_WORDS`. Memoize
`catalog_vocabulary` (also M7).

### [ ] S5. Drop app-level rate-limit / `response_format` fallback for OpenAI

Covered by H5. Close this when H5 lands.

### [ ] S6. Merge the three execute backends

`engine/execute.py`, `postgres.py`, `databricks.py` →
`run_with_policy(backend, compiled, opts)` plus a ~20-line `Backend` protocol
(`connect`, `execute`, `interrupt`). Removes 3× `_safe_close`, `_execute_once`,
audit assembly.

### [ ] S7. One BFS for join paths

`kernel/joins.approved_path` and `resolve_joins` →
`shortest_join_path(catalog, a, b) -> list[JoinKey] | Ambiguous | Unreachable`.

### [ ] S8. One grain check

`kernel/joins.fan_out_errors` and `sql_validate._check_fan_out` →
`grain_check(base_table, joins, aggregates)`. Do this after H3 / H6.

### [ ] S9. One canonical plan JSON

`LogicalPlan.to_json` and `compile._hash_plan` → `canonical_json(plan)`.

### [ ] S10. Pick one planner path

STATUS: SQL path is +16 pts right on Spider at a similar wrong rate. The UI
tagline "the model never writes SQL" is already false. Keep LQP only if
`explain.py` is a product requirement; otherwise retire the IR + 777-line
validator. Needs an ADR update if LQP is dropped.

### [ ] S11. Replace `SYSTEM_PROMPT` shape prose with the Pydantic schema

Covered by M6. Close this when M6 lands.

### [ ] S12. Guards return `(code, message)`

Replace `planner/clarify.py` `code_from_guard_message` substring matching (L8).

### [ ] S13. Delete trivial metric helpers

`metrics_for_catalog(c)` (`return list(c.metrics)`) and `metric_tables` via
`_parse_agg(f"{m}:_")` → attribute access + a real expression parser.

### [ ] S14. Stop production code importing `demo`

`engine/runtime.py:16` → `demo.load_chinook`; `engine/domains.py:14` →
`demo.chinook`; `_BUILTIN_BUILDERS` populated by import side effect in
`demo/chinook.py` (also L7). Invert: demo registers into engine, or Chinook
is data-only JSON.

---

## Wave 6 — hygiene

### [ ] L1. `demo/ask.py` prints each row twice (187–188)

### [ ] L2. Dead code in `kernel/validate.py`

Vestigial `for label, filters in (("filters", plan.filters),):` (~440) and
`except CompilationError: raise` (~641).

### [ ] L3. One source of truth for default model and package version

`"gpt-4o-mini"` in `llm.py:53` and `:346`. Version `0.4.0` in `pyproject.toml`
and `api/http.py`; installed editable still reports `0.1.0` until M11.

### [ ] L4. Tests that need Chinook must `skipif` the missing file

`tests/test_compile.py:571`, `tests/test_joins.py:20`, `tests/test_metrics.py:26`.

### [ ] L5. Load `.env` in one place; pass the key through compose

No `python-dotenv` anywhere. `docker-compose.yml` does not pass
`OPENAI_API_KEY`. `Dockerfile` bakes `SECURE_QUERY_AUTH_MODE=dev` and downloads
Chinook at start. Load dotenv in the process entrypoint only; do not bake keys.

### [ ] L6. Lock dependencies

No lockfile; `sqlglot>=23` (installed 30.16). Add `uv lock` or `pip-compile`.

### [ ] L10. Memoize `catalog.table_map()` / `column_map()`

`functools.cached_property` on a frozen `Catalog`.

### [ ] L11. CI: cache Chinook; broaden the no-f-string-SQL check

`load_chinook --strict` downloads every run. The grep covers only `compile.py`;
extend to `sql_validate.py` and the three backends (or an AST/ruff rule).

### [ ] L12. Fix stale paths in `docs/SECURITY.md`

Still references `sample_catalog.py` and `auth.py`.

---

## Index

| ID | Title | Wave | Status |
|----|-------|------|--------|
| C1 | Pin confirm → run | 1 | done |
| H1 | Integer literal on float | 1 | done |
| H2 | Trend order by time bucket | 1 | done |
| H3 | Child-filter list grain | 1 | done |
| H6 | CTE fan-out bypass | 1 | done |
| H4 | Startup singletons | 2 | done |
| H5 | OpenAI timeout / retries | 2 | done |
| M5 | Stable catalog prefix | 2 | done |
| M6 | Structured outputs | 2 | done |
| H7 | PG/DBX read-only | 3 | done |
| M1 | Hash questions in audit | 3 | done |
| M2 | Generic client errors | 3 | done |
| M3 | Cancel on timeout | 3 | done |
| L13 | Header mode fail-closed | 3 | done |
| M4 | Compile cache lock | 4 | done |
| M8 | SQL dialect from catalog | 4 | done |
| M9 | Mock client SQL marker | 4 | done |
| M10 | Limit unbounded CTEs | 4 | done |
| M11 | Fix editable install | 4 | done |
| M12 | No FastAPI on core import | 4 | done |
| S1–S14 | Simplify | 5 | open |
| L1–L12 | Hygiene | 6 | open |

M7 is tracked as **S4**. L7 as **S14**. L8 as **S12**. L9 as **S2**.
