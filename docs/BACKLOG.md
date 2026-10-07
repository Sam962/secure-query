# Review backlog — 2026-10-06

Work items from the independent review. Detail and evidence live in
[`REVIEW-2026-10-06.md`](REVIEW-2026-10-06.md). Do not retune prompts or guards on
holdout case ids ([EVAL.md](EVAL.md)).

**Status:** Waves 1–6 merged (2026-10-07).
Check a box when the change is in the tree; leave the ID stable.

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

### [x] S1. Delete `kernel/builder.py`

Moved to `demo/lqp.py` (tests/demo helper). Kernel no longer exports `LQP`.

### [x] S2. Delete Chinook presentation from the service layer

Canned Chinook phrases removed from `suggest.py`. `result_column_units` uses
`metric.group_by` labels, not `{"country","name","title"}`. `money_scale_note`
stays as generic USD scale (catalog unit), not Chinook-specific.

### [x] S3. Move `evals/consistency.py` and `evals/adjudicate.py` out of the package

Now `scripts/consistency.py` and `scripts/adjudicate.py`.

### [x] S4. Drop guards with negative net value on the current model

Done: `_STOP_WORDS` deduped; `catalog_vocabulary` memoized (M7). LQP keeps
`dropped_concepts` and `opaque_grouping_keys`: measured net-positive there
(Chinook 12 stopped-wrong / 0 cost-right; Spider 80 / 9). Open: on the SQL path
with gpt-4.1-mini, `dropped_concept` is 11 / 32 and `dropped_filter` 1 / 11
(STATUS 2026-10-06).

Decided 2026-10-07: keep both on the SQL path. Adjudicated, `dropped_concept`
stopped 7 real errors on Spider (real wrong 2.75% → 4.5% without it).
`dropped_filter` stopped none on Spider (cost 11 right), but on the curated
suites, gpt-4.1-mini SQL path, it prevented 6 wrong answers (should-decline +
stopped-wrong) for 4 right: Chinook dev 3/2, Northwind 2/1, Northwind dev 1/1.

### [x] S5. Drop app-level rate-limit / `response_format` fallback for OpenAI

Covered by H5.

### [x] S6. Merge the three execute backends

Shared `run_with_policy(compiled, runner=...)` plus one `_safe_close`. Connect /
execute / interrupt stay per-driver (DuckDB thread+interrupt, PG session timeout,
Databricks cursor.cancel).

### [x] S7. One BFS for join paths

`_bfs`; `approved_path` and `resolve_joins` both use it.

### [x] S8. One grain check

`joins.grain_ok(to_one, table)` used by `fan_out_errors` and `_check_fan_out`.

### [x] S9. One canonical plan JSON

`LogicalPlan.to_json` is the canonical form; `_hash_plan` hashes it.

### [x] S10. Pick one planner path

**Keep both.** ADR: `docs/adr/004-keep-both-planners.md`. LQP stays for
explain-back and governed metrics; SQL stays for coverage.

### [x] S11. Replace `SYSTEM_PROMPT` shape prose with the Pydantic schema

Covered by M6.

### [x] S12. Guards return `(code, message)`

Public guards return `GuardHit`. `code_from_guard_message` remains only for
model-written `PlannerRefusal` reasons.

### [x] S13. Delete trivial metric helpers

`metrics_for_catalog` deleted; callers use `catalog.metrics`. `metric_tables`
parses measures via `_parse_measure`.

### [x] S14. Stop production code importing `demo`

Only the sample domain imports `demo.chinook`, lazily inside
`load_domain_catalog`; importing the API loads no demo code. `runtime.py` uses
`SECURE_QUERY_DUCKDB` / `data/chinook.duckdb`.

---

## Wave 6 — hygiene

### [x] L1. `demo/ask.py` prints each row twice (187–188)

Duplicate `print` removed.

### [x] L2. Dead code in `kernel/validate.py`

Vestigial filter-label loop and `except CompilationError: raise` removed.

### [x] L3. One source of truth for default model and package version

`planner.llm.DEFAULT_MODEL`. API version from `importlib.metadata`.

### [x] L4. Tests that need Chinook must `skipif` the missing file

`tests.conftest.requires_chinook` on compile/joins/metrics DB probes.

### [x] L5. Load `.env` in one place; pass the key through compose

`engine.env.load_dotenv` from API lifespan, `demo.ask`, and `evals.run`.
Compose passes `OPENAI_API_KEY` / provider / model from the host env.

### [x] L6. Lock dependencies

`requirements.lock` from `pip-compile` / pinned extras (see file).

### [x] L10. Memoize `catalog.table_map()` / `column_map()`

The per-instance cache in the tree is unsafe: `model_copy(update=...)` copies
`__dict__` without revalidation, so a copied catalog keeps its parent's tables
(used by the dialect and overlay paths). The dict build costs ~0.5 µs. Closed
without a cache (2026-10-07).

### [x] L11. CI: cache Chinook; broaden the no-f-string-SQL check

`actions/cache` on `data/chinook.duckdb`. Grep covers compile, sql_validate,
and the three execute backends.

### [x] L12. Fix stale paths in `docs/SECURITY.md`

Now `demo/chinook.py` and `auth/__init__.py`.

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
| S1 | Builder out of kernel | 5 | done |
| S2 | Chinook presentation | 5 | done |
| S3 | Evals scripts moved | 5 | done |
| S4 | Drop negative-net guards | 5 | done (kept, measured) |
| S5 | OpenAI fallback (via H5) | 5 | done |
| S6 | Shared execute policy | 5 | done |
| S7 | One join BFS | 5 | done |
| S8 | One grain check | 5 | done |
| S9 | One canonical plan JSON | 5 | done |
| S10 | Keep both planners (ADR 004) | 5 | done |
| S11 | Schema prompt (via M6) | 5 | done |
| S12 | Guard (code, message) | 5 | done |
| S13 | Trivial metric helpers | 5 | done |
| S14 | Demo registers into engine | 5 | done |
| L1 | Double-print rows | 6 | done |
| L2 | Dead validate code | 6 | done |
| L3 | Model / version source | 6 | done |
| L4 | skipif Chinook | 6 | done |
| L5 | dotenv + compose key | 6 | done |
| L6 | Lockfile | 6 | done |
| L10 | Memoize table_map | 6 | won't do (unsafe, no gain) |
| L11 | CI cache + SQL grep | 6 | done |
| L12 | SECURITY.md paths | 6 | done |

M7 is tracked as **S4**. L7 as **S14**. L8 as **S12**. L9 as **S2**.

---

## Recheck against REVIEW-2026-10-06 (2026-10-07)

Uncommitted Waves 5–6 on `review-wave-4` (Wave 4 is PR #3; not merged). No commit.

**Restest:** `ruff check` clean. `PYTHONPATH=src python -m pytest` **528 passed**.
Mock holdout LQP and SQL: **0 wrong**, 50% abstain (gate). `import secure_query`
does not load FastAPI. Installed version `0.4.0` matches `pyproject.toml`.

**Original review probes (re-run):**

| Probe | Result |
|-------|--------|
| H1 `Total > 5` integer and float literals | both validate |
| H2 monthly revenue ordered by `InvoiceDate` | compiles `DATE_TRUNC` + `ORDER BY` |
| H3 Customer + Invoice.BillingCountry='USA' | **13** rows, `EXISTS` |
| H6 `WITH i AS (…) SELECT … SUM(Customer.SupportRepId) JOIN i` | `sql.fan_out` |

**Review scorecard vs 2026-10-06 (what the review would say now):**

| Dimension | Then | Now | Why |
|-----------|------|-----|-----|
| Correctness | 6 | **8** | C1 / H1 / H2 / H3 / H6 closed. Confirm→run pins `plan_hash`. |
| Security | 7 | **8** | H7/M1/M2/M3/L13 closed. Remaining: warehouse identity is still an ops concern. |
| Performance | 5 | **7** | H4/H5/M4/M5 closed. Per-query DuckDB connect remains. |
| Simplicity | 4 | **6** | Builder out of kernel; eval scripts moved; shared execute/BFS/grain/JSON; demo registers into engine. Two planner paths **kept** (ADR 004). Guard layer still large. |
| Maintainability | 6 | **8** | Import graph, lockfile, dotenv, CI cache, version/model single source, guard codes, SECURITY.md paths. |

Open by design: S10 did **not** retire LQP. `dropped_concepts` / `opaque_grouping_keys`
are off the default post-plan list but the functions remain. `money_scale_note` stays
as generic USD scale. `code_from_guard_message` remains only for model refusals.
