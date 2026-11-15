# Plan — Trusted Evidence Kernel

**Updated:** 2026-11-14 · Measured results live in [STATUS.md](STATUS.md) · Start at [HANDOVER.md](HANDOVER.md).

## Goal

On **one real Databricks domain**, with equal curation effort, Secure Query is wrong
measurably less often than Databricks Genie, at an answer-rate users accept, and every
answer or refusal ships a **signed Evidence Bundle**.

| Metric | Bar |
|--------|-----|
| Wrong-rate, 95% upper bound | ≤ 2%, and below Genie's |
| Answer-rate on answerable questions | ≥ 70% |
| Holdout | ≥ 200 real questions, 5 repeats |
| Evidence | Every response has a bundle that verifies |

What we may claim today: **model SQL is validated against an allowlist and regenerated;
it never runs as written.** Everything else is earned by measurement.

## Decisions locked (do not reopen without Sam)

- **Wedge:** Databricks + Unity Catalog first. Postgres via Lakehouse Federation.
  SharePoint **lists** via Lakeflow Connect → Delta. No document Q&A.
- **Planner:** validated SQL is the production path (`SECURE_QUERY_PLANNER=sql`, ADR 005).
  The LogicalPlan path stays for catalog-owned metrics and explain helpers.
- **Security:** the allowlist validator (`kernel/sql_validate.py`) is the safety story.
  Keyword guards are never the safety story.
- **Jev / TypeSafe:** no dependency. Revisit only with a measured lift (parking lot P2).

---

## How an agent works this plan (read every time)

1. Do the **first task below whose box is `[ ]` and whose "Blocked by" is empty or done.**
   Never skip ahead. Never do two tasks in one change.
2. Do the setup in [HANDOVER.md](HANDOVER.md) first. Run the **Verify** block before you
   start, so you know the baseline is green.
3. Change only the files the task lists. If you think another file must change, stop and
   say why in your report instead of changing it.
4. When the task's **Done when** checks all pass, run the **Verify** block again.
5. Tick the task's box here, and add one dated line under "Changelog" in STATUS.md with
   the numbers you measured (if the task measures anything).
6. Do not commit, push, or open a PR unless Sam asked you to. Never add AI co-author
   trailers to commits.
7. Stop and report: what changed (files), the Verify output (pass counts), any numbers.

**Never, in any task:** tune prompts or code against holdout case ids; put dataset names
or domain words in code (domain knowledge goes in catalog JSON); let an LLM repair a
warehouse error; claim a wrong-rate that was not adjudicated; call a model's rating
"human".

### Verify block (run from the repo root)

```bash
source .venv/bin/activate
export PYTHONPATH=src
ruff check .
pytest -q
python -c "import secure_query, sys; assert 'fastapi' not in sys.modules"
python -m secure_query.evals.run --split holdout --provider mock --planner lqp
python -m secure_query.evals.run --split holdout --provider mock --planner sql
```

Pass means: ruff prints `All checks passed!`, pytest shows `0 failed`, and both holdout
runs exit 0 (`echo $?` prints `0`).

---

## Phase 0 — Sam

- [x] **0.1 Review and commit the 2026-11-11 cleanup.** Owner: Sam. The working tree holds
  the last two sessions of work, uncommitted. Read `git status` and `git diff`, then commit
  on `trusted-core`. A full backup of the tree from before the cleanup was taken; see
  STATUS.md changelog.

## Phase 1 — Measurements we can trust (agent; no Databricks needed)

- [x] **1.1 Make the default model the one we measured.**
  - Why: STATUS numbers are for `gpt-4.1-mini`; the code default is `gpt-4o-mini`.
  - Files: `src/secure_query/planner/llm.py` (`DEFAULT_MODEL`), `.env.example`, `README.md`.
  - Steps: set `DEFAULT_MODEL = "gpt-4.1-mini"`. In `.env.example`, replace the two
    Ollama default lines with `# SECURE_QUERY_PROVIDER=openai` and
    `# SECURE_QUERY_MODEL=gpt-4.1-mini`. In README, state OpenAI `gpt-4.1-mini` as the
    measured default and Ollama as a local option.
  - Done when: `grep -n DEFAULT_MODEL src/secure_query/planner/llm.py` shows `gpt-4.1-mini`,
    and Verify passes.

- [x] **1.2 Production refuses to start without a signing secret.**
  - Why: without `SECURE_QUERY_REVIEW_SECRET` each process signs with a random key, so
    Evidence Bundles stop verifying after a restart or on another worker.
  - Files: `src/secure_query/auth/__init__.py` (`check_auth_config`), `tests/test_auth.py`.
  - Steps: in `check_auth_config`, when `SECURE_QUERY_ENV=production` and
    `SECURE_QUERY_REVIEW_SECRET` is empty or shorter than 32 characters, raise the same
    error type it raises for other unsafe configs, with a message naming the variable.
    Add two tests: production without the secret raises; production with a 32-character
    secret passes.
  - Done when: the two new tests pass and Verify passes.

- [x] **1.3 Evidence lists the checks that actually ran.**
  - Why: `policy_checks` is a fixed tuple today, the same for every path, so a bundle can
    claim a check that did not run.
  - Files: `src/secure_query/kernel/evidence.py`, `src/secure_query/api/service.py`,
    `tests/test_evidence.py`.
  - Steps:
    1. In `evidence.py`, replace `_DEFAULT_CHECKS` with three named tuples:
       `SQL_PATH_CHECKS` (allowlist_columns, no_pii_high, approved_joins, fan_out,
       row_filters, read_only, limit_capped), `PLAN_PATH_CHECKS` (catalog_columns,
       no_pii_high, approved_joins, row_filters, read_only, limit_capped) and
       `METRIC_CHECKS` (approved_metric, read_only). Make `policy_checks` a required
       argument of `build_evidence` for kinds `ok` and `confirm`.
    2. In `service.py`, pass the tuple for the path that produced the query: SQL path,
       LogicalPlan path, or approved metric. Thread it into `_finish` as a parameter.
    3. Test: an SQL-path confirm returns `SQL_PATH_CHECKS`; a builtin metric returns
       `METRIC_CHECKS`.
  - Done when: `grep -n _DEFAULT_CHECKS -r src` prints nothing, tests pass, Verify passes.

- [x] **1.4 Measure the table-confidence gate, then decide its default.**
  - Why: `planner/confidence.py` refuses when lexical table-linking is unsure. It is on
    for any catalog wider than 16 tables, so every real warehouse domain gets it, and it
    was never measured. Forced on for Chinook it refused **4 of 25 answerable questions
    (16%)**, all multi-table.
  - Files: `src/secure_query/planner/clarify.py`, `src/secure_query/api/service.py`,
    `src/secure_query/planner/confidence.py`, `.env.example`, `docs/STATUS.md`.
  - Steps:
    1. Give the gate its own refusal code so it can be counted: add `"low_link_confidence"`
       to `ClarifyCode` in `clarify.py`; in `service.ask`, use it instead of
       `"validation_failed"` for the low-confidence refusal.
    2. Measure without an LLM. In a scratch script (do not commit it), build one wide
       catalog by concatenating the `tables` and `join_keys` of `sample_catalog()` and
       `src/secure_query/evals/suites/northwind/catalog.json` (more than 16 tables). For
       every `expect="answer"` case in `suites/chinook/cases.json` and
       `suites/northwind/cases.json`, call `link_tables(question, wide, k=8)` and count
       `confidence < confidence_threshold()`.
    3. Decision rule: if more than 5% of answerable questions would be refused, change the
       default in `confidence_threshold()` from `0.55` to `0.0` (gate off unless the env
       var sets it) and set `.env.example` to `# SECURE_QUERY_LINK_CONFIDENCE=0  # off;
       enable only after a live run shows it prevents wrong answers`. Otherwise leave it.
    4. Record the count, the rate and the decision in STATUS.md.
  - Done when: STATUS.md has the numbers and decision, and Verify passes.

- [x] **1.5 Measure DPC-lite, then keep or delete it.**
  - Why: `planner/dpc_lite.py` re-runs the same validated SQL twice and compares
    `COUNT(*)` with `COUNT(1)`. Both checks are equal by SQL semantics unless the query is
    nondeterministic, and ties are already made deterministic. It costs 2–3 extra
    warehouse queries per question.
  - Blocked by: 1.4. Needs `OPENAI_API_KEY` (cost: a few dollars).
  - Files: `src/secure_query/planner/clarify.py`, `src/secure_query/planner/sql_plan.py`,
    then possibly `planner/dpc_lite.py`, `tests/test_evidence.py`, `docs/STATUS.md`.
  - Steps:
    1. Add `"unstable_probe"` to `ClarifyCode`. In `sql_plan.py`, use it for the refusal
       that comes from `dpc_lite_check` (keep `"unstable_answer"` for the two-readings gate).
    2. Run, with the key set:
       ```bash
       python -m secure_query.evals.run --split dev --planner sql --provider openai --json /tmp/dpc-chinook.json
       python -m secure_query.evals.run --suite src/secure_query/evals/suites/northwind_dev/cases.json --planner sql --provider openai --json /tmp/dpc-northwind.json
       ```
       Count attempts whose refusal code is `unstable_probe`.
    3. Decision rule: if the count is 0, delete `planner/dpc_lite.py`, its call in
       `sql_plan.py` and its tests, and keep the gate fingerprints from the two-readings
       check only. If the count is above 0, keep it and list each case id in STATUS.md
       with what made the answer unstable.
  - Done when: STATUS.md has the count and decision, and Verify passes.

- [x] **1.6 Plan the two readings at the same time.**
  - Why: with the self-consistency gate on, each question waits for two LLM calls one
    after the other. Running them together roughly halves that wait.
  - Blocked by: 1.5.
  - Files: `src/secure_query/planner/sql_plan.py`, `tests/test_sql_validate.py`,
    `docs/STATUS.md`.
  - Steps:
    1. In `plan_sql_question`, when `check_client` and `value_probe` are both set, start
       the second reading in a `concurrent.futures.ThreadPoolExecutor(max_workers=1)` before
       the first reading's loop, and call `.result()` where the code calls
       `plan_sql_question(... check_client ...)` today. Results and refusal codes must be
       identical to the sequential version.
    2. If the first reading ends in a refusal or an error, cancel the future if it has not
       started; otherwise let it finish and discard it.
    3. Test with two mock clients that each `time.sleep(0.2)`: the gated call returns in
       under 0.35 s, and the existing gate tests still pass.
    4. Measure median seconds per question on 20 Chinook dev questions with a live model,
       before and after, and record both in STATUS.md.
  - Done when: the timing test passes, STATUS.md has both medians, and Verify passes.

- [x] **1.7 Adjudicate the new default model, `gpt-6-luna`.**
  - Why: OpenAI's deprecations page (read 2026-11-11) lists `gpt-4.1-mini-2025-04-14` for
    shutdown on 2027-01-14 and names `gpt-6-luna` as the replacement. Every number in
    STATUS.md is for `gpt-4.1-mini`. The client now accepts any model (it adapts to the
    parameters a model rejects), so this is a measurement task, not a code task.
  - Measured so far (2026-11-11, Chinook dev 134 cases / Northwind dev 10, not adjudicated,
    wrong answers): `gpt-4.1-mini` 7 / 3; `gpt-6.1-terra` reasoning off 11 / 4; `gpt-6-luna`
    reasoning off 13 / 4; `gpt-6-luna` reasoning on 13 / 4 (plus 1 empty reply). Most of the
    gap is one behaviour: the newer models answer "which X has the most Y" with X only and
    leave out Y (6 of terra's 11), which the strict grader scores wrong.
  - Fixed 2026-11-11: the pipeline now adds the measure a grouped query ranks or filters by
    (`show_ranking_measure`), for every model. Re-measured, same suites: `gpt-4.1-mini`
    6 / 1; `gpt-6-luna` reasoning off 5 / 1; `gpt-6-luna` reasoning on 3 / 1 (plus 1 empty
    reply); `gpt-6.1-terra` 5 / 2. All candidates now qualify on dev. Steps 1 and 2 are
    done (the fix replaced the prompt line).
  - Decided 2026-11-11 (Sam): `DEFAULT_MODEL` is `gpt-6-luna` with reasoning off, on the dev
    numbers. Spider test 400 was then run (STATUS changelog, 2026-11-11): `gpt-6-luna` has
    29 wrong against 33 for `gpt-4.1-mini`, but answers 67.3% against 76.5%, under the 70%
    bar. Fixed 2026-11-12 on Spider dev (validator rejected correlated subqueries; refusal
    rule in the prompt): on Spider test 400 `gpt-6-luna` is now 307 right / 40 wrong / 53
    refused (76.8%) against 310 / 39 / 51 for `gpt-4.1-mini`. The default stays
    `gpt-6-luna`. Adjudicated 2026-11-12: 14 real errors of 400 (3.5%, CI 2.1–5.8%), by two
    model raters; no human rated this round.
  - Files: `src/secure_query/planner/sql_plan.py` (prompt only), `src/secure_query/planner/llm.py`
    (`DEFAULT_MODEL`), `.env.example`, `README.md`, `docs/STATUS.md`.
  - Steps:
    1. Follow "Qualifying a new model" in [EVAL.md](EVAL.md) for each candidate.
    2. If the missing-measure pattern is still the main gap, try one dataset-agnostic prompt
       line on **dev only** (return the value a ranking or filter is based on, next to the
       name), and re-run every candidate and `gpt-4.1-mini` with it.
    3. Pick the cheapest candidate whose dev wrong-rate is not above `gpt-4.1-mini`'s.
       Re-run Spider test 400 with it and adjudicate, then change `DEFAULT_MODEL` and the
       docs. If none qualifies, stop and report.
  - Done when: STATUS.md has the adjudicated Spider numbers for the new default, and
    Verify passes.

## Phase 2 — A fair head-to-head harness (agent; no Databricks needed)

- [x] **2.1 Run the scorecard against the warehouse, not a local file.**
  - Why: `evals/scorecard.py` runs reference SQL and Secure Query on local DuckDB while
    Genie answers from the warehouse. For a real domain there is no local copy, and for
    a fair comparison both systems and the references must read the same data.
  - Files: `src/secure_query/evals/accuracy.py`, `src/secure_query/evals/scorecard.py`,
    `tests/test_accuracy_harness.py`.
  - Steps:
    1. In `accuracy.py`, add `RowRunner = Callable[[str], list[tuple]]`, a function from
       SQL text to rows. Add an optional `run_sql: RowRunner | None = None` parameter to
       `run_live_case`, `_run_once`, `_judge`, `_reference_rows`,
       `_unlimited_reference_rows` and `_run_reference`. When it is `None`, behave exactly
       as today (DuckDB at `db_path`). When it is set, every execution goes through it.
    2. In `scorecard.py`, add `--backend {duckdb,databricks}` (default `duckdb`). For
       `databricks`, define `run_sql(sql)` as
       `execute_databricks(CompiledQuery(sql=sql, plan_hash="eval", sql_hash="eval", parameters=[]), principal=Principal(principal_id="eval", tenant_id=catalog.tenant_id, warehouse_token=os.environ["SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN"]), options=ExecuteOptions(max_rows=ROW_CAP)).rows`
       and pass it to both systems. Reference SQL runs unvalidated here; that is
       acceptable only because it is eval-only and the token is SELECT-only.
    3. Test: a fake `run_sql` that records every SQL string proves that no DuckDB file is
       opened when `run_sql` is given (point `db_path` at a path that does not exist).
  - Done when: the new test passes, a `--backend duckdb` run gives the same scorecard as
    before, and Verify passes.

- [x] **2.2 Export every wrong answer from both systems for adjudication.**
  - Why: the bar is an adjudicated wrong-rate, and today `--out` writes only totals.
  - Blocked by: 2.1.
  - Files: `src/secure_query/evals/scorecard.py`, `scripts/adjudicate.py`,
    `tests/test_genie.py`.
  - Steps:
    1. Make `--out` also write, per system, every case with its attempts (verdict, detail,
       SQL, fingerprint), in the same JSON shape `evals.run --json` writes.
    2. Make `scripts/adjudicate.py packets` accept that file and build packets for both
       systems, labelled by system, without showing the rater which system produced it.
  - Done when: a mock run with `--sq-only --out x.json` produces a file that
    `python scripts/adjudicate.py packets x.json -o p.json` turns into packets, and Verify
    passes.

## Phase 3 — Live head-to-head (blocked on Sam)

- [ ] **3.1 Sam: domain, date and access.** Pick the domain and the partner date. Provide a
  Databricks workspace, a SQL warehouse, a Genie space over the same tables, and the
  credentials in `.env` (see `.env.example`). Prove Genie answers one question by hand.
- [ ] **3.2 Draft and approve the catalog.** Blocked by 3.1. Run
  `fetch_unity_catalog_draft` for the domain schema, save the draft as
  `config/<domain>-catalog.json`, then **a named owner** reviews PII flags and
  `join_keys`. Fill [OWNERSHIP.md](OWNERSHIP.md). An agent may draft; only the owner approves.
- [ ] **3.3 Live smoke.** Blocked by 3.2. `python scripts/databricks_smoke.py` exits 0.
- [ ] **3.4 Freeze the holdout.** Blocked by 3.1. Sam and the domain owner write at least
  200 real questions with reference SQL (and expected refusals), following
  [EVAL.md](EVAL.md). Freeze the file before any system sees it.
- [ ] **3.5 Run and adjudicate.** Blocked by 2.2, 3.3, 3.4.
  ```bash
  python -m secure_query.evals.scorecard --suite <holdout.json> --backend databricks --repeat 5 --out docs/baselines/genie-h2h-<date>.json
  ```
  Adjudicate every wrong answer from both systems the same way, then record the table in
  STATUS.md. The pass bar is the Goal table above.
  Also audit a random sample of at least 60 answers scored right, per system: a match on
  one database does not prove the query is right (SpotIt, arXiv 2510.26840; first audit
  in STATUS.md, 2026-11-14).

---

## Phase 4 — Production hardening (agent; no Databricks needed)

Sam asked on 2026-11-13 for the architecture review's findings to be fixed, ahead of Phase 2.
Each task is one commit. Every new control is off or permissive by default outside
`SECURE_QUERY_ENV=production`, so the demo keeps working.

- [x] **4.1 Signed reviews expire; signing keys rotate.** A review carries `expires_at`
  (default 15 minutes, `SECURE_QUERY_REVIEW_TTL_SECONDS`) inside the signed payload.
  `SECURE_QUERY_REVIEW_SECRET_PREVIOUS` is accepted for verification only, so a key can be
  rotated without failing reviews in flight. Files: `api/service.py`, `api/http.py`, tests.
- [x] **4.2 Evidence an auditor can verify without the secret.** With
  `SECURE_QUERY_EVIDENCE_PRIVATE_KEY` (Ed25519, PEM) bundles are signed asymmetrically and
  carry a `key_id`; `GET /evidence/public-key` serves the public key. HMAC stays the
  fallback. Files: `kernel/evidence.py`, `api/http.py`, `pyproject.toml`, tests.
- [x] **4.3 OIDC sign-in.** `SECURE_QUERY_AUTH_MODE=jwt` validates a Bearer JWT against the
  identity provider's keys (issuer, audience, expiry) and maps a group claim to tables.
  Files: `auth/__init__.py`, `pyproject.toml`, tests.
- [x] **4.4 Access changes without a restart.** The principals file is re-read when it
  changes. Files: `auth/__init__.py`, tests.
- [x] **4.5 Audit trail that shows tampering.** Trace and audit lines are hash-chained and
  written under a lock; `SECURE_QUERY_AUDIT_STDOUT=1` also emits them as JSON on stdout for
  a log shipper; `scripts/verify_audit.py` checks a file. Files: `kernel/trace.py`,
  `engine/audit.py`, script, tests.
- [x] **4.6 Operational surface.** Per-person rate limit, `GET /metrics` (Prometheus text),
  a request id on every response, and a breaker that fails fast when the model provider is
  down. Files: `api/http.py`, new `api/ops.py`, `planner/llm.py`, tests.
- [x] **4.7 Reuse warehouse connections.** One Databricks connection per credential, reused
  across the lookups and the query of one ask. Files: `engine/databricks.py`, tests.
- [x] **4.8 The gate compares whole answers.** Check what the 50-row probe misses and fix
  it. Files: `planner/sql_plan.py`, `api/service.py`, tests.
- [x] **4.9 Catalog lifecycle.** The catalog records its version and who approved it and
  when; production refuses an unapproved catalog; a drift check compares it with the
  warehouse. Files: `kernel/catalog.py`, `engine/catalog_draft.py`, tests.
- [x] **4.10 Production image.** Non-root, pinned from `requirements.lock`, no demo data or
  dev auth baked in. Files: `Dockerfile`, `docker-compose.yml`.
- [x] **4.11 Say what leaves the building.** SECURITY.md lists exactly what is sent to the
  model provider and how to use a private endpoint. Files: `docs/SECURITY.md`.
- [x] **4.12 The consistency gate runs for API and CLI requests.** Found on 2026-11-13 by a
  live end-to-end check: `ask()` skipped the second reading whenever a client was passed
  in, and both the API and the CLI pass one in. Only the eval harness ran the gate. The
  model client now supplies its own second reader (`check_twin`). Files: `planner/llm.py`,
  `api/service.py`, `tests/test_service.py`.
- [x] **4.13 Docs.** HANDOVER, STATUS, README, SECURITY, `.env.example` describe the above.

Not started, because each is a product decision rather than a fix: see P4–P6 below.

## Parking lot (needs Sam's decision; do not start)

- **P1. Retire the LogicalPlan question planner — after P4.** Looked at on 2026-11-13 and
  not done: it is today the only route from a question to a catalog metric (the SQL path
  never picks a `metric_id`), and the mock planner behind most API and service tests runs
  through it. Removing it first would drop metrics from the product. Build P4, move the
  tests to the SQL path, then delete.
- **P1 (original note). Retire the LogicalPlan question planner.** It is off by default since ADR 005 but
  still carries about 2,000 lines: `planner/plan.py`, `planner/guard.py` (843 lines),
  `planner/prompt.py`, `planner/response_schema.py`, the `lqp` option in `evals/run.py`,
  and guard use in `planner/suggest.py`. Metrics and `kernel/explain.py` would stay.
- **P2. Jev / TypeSafe schema linker.** Only with a measured lift over the local linker on
  a wide catalog. The unused hook was removed on 2026-11-11.
- **P3. Document Q&A over SharePoint.** Out of scope for this product.
- **P4. Certified answers.** Route common questions to analyst-approved queries and metrics
  before free SQL. The likeliest way to the 2% wrong-rate goal; needs a matching rule and a
  measured before/after on a real domain.
- **P5. Sensitivity levels and masking.** PII is high or not, per column. Roles, masking and
  checks on returned rows need the customer's data classification.
- **P6. Conversation.** Follow-up questions and a feedback loop from users into the dev suite.
- **P8. A different model as the gate's second reader.** `SECURE_QUERY_CHECK_MODEL` exists
  and is off. On Spider dev 400 with `gpt-6.1-terra` it cut wrong answers from 42 to 29 and
  right answers from 300 to 281 (STATUS, 2026-11-14). To turn it on: one confirming run on
  Spider test 400, adjudicated, and Sam's call on the answer-rate it costs.
- **P9. NULL ordering on Databricks.** `ORDER BY x LIMIT 1` for "the lowest X" returns a
  NULL row on Databricks when the column has one (NULL sorts first ascending there, last on
  DuckDB). Check on the real warehouse in 3.3 before changing anything; Genie may do the same.
- **P10. Row caps in the head-to-head.** Done for Spider suites on 2026-11-15: the suite
  script leaves out an answer over 5,000 rows, an undetermined gold column and a gold LIMIT
  that cuts a tie. Apply the same three rules when the holdout is frozen in 3.4. Still
  open, a product question: neither system told the user its answer was cut.
- **P11. Refusals from a missing join.** On `bakery_1` 31 of 94 questions were refused
  because the drafted catalog lacked one join the source schema does not declare (STATUS
  2026-11-15). In 3.2 the owner's catalog review must cover joins, and the refusal text
  should name the missing join so the owner can approve it.
- **P7. PII detection from column values.** The catalog draft flags PII from Unity Catalog
  tags and column names only. Add value sampling only if the owner's review in 3.2 finds
  personal data in columns that neither a tag nor the name gave away.
