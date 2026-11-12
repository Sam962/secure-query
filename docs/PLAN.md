# Plan — Trusted Evidence Kernel

**Updated:** 2026-11-11 · Measured results live in [STATUS.md](STATUS.md) · Start at [HANDOVER.md](HANDOVER.md).

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

- [ ] **1.6 Plan the two readings at the same time.**
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

## Phase 2 — A fair head-to-head harness (agent; no Databricks needed)

- [ ] **2.1 Run the scorecard against the warehouse, not a local file.**
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

- [ ] **2.2 Export every wrong answer from both systems for adjudication.**
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

---

## Parking lot (needs Sam's decision; do not start)

- **P1. Retire the LogicalPlan question planner.** It is off by default since ADR 005 but
  still carries about 2,000 lines: `planner/plan.py`, `planner/guard.py` (843 lines),
  `planner/prompt.py`, `planner/response_schema.py`, the `lqp` option in `evals/run.py`,
  and guard use in `planner/suggest.py`. Metrics and `kernel/explain.py` would stay.
- **P2. Jev / TypeSafe schema linker.** Only with a measured lift over the local linker on
  a wide catalog. The unused hook was removed on 2026-11-11.
- **P3. Document Q&A over SharePoint.** Out of scope for this product.
- **P4. Move off `gpt-4.1-mini` before OpenAI removes it.** OpenAI's deprecations page
  (read 2026-11-11) lists `gpt-4.1-mini-2025-04-14` for shutdown on **2027-01-14**, with
  `gpt-6-luna` as the recommended replacement (`gpt-4o-mini` and `gpt-4.1` go the same
  day). Every number in STATUS.md is for `gpt-4.1-mini`, so this is a re-measurement, not
  a config change: run Chinook dev and Northwind dev with the candidate
  (`SECURE_QUERY_MODEL=<candidate>`), compare wrong-rate first, then answer-rate, latency
  and cost against the 2026-11-11 runs; switch `DEFAULT_MODEL` only if wrong-rate does not
  rise; then redo Spider 400 and its adjudication. Check first that the candidate accepts
  the `temperature` setting the second reading uses (`CHECK_TEMPERATURE`); the GPT-6
  models are listed as reasoning models.
  First look, 2026-11-11, `gpt-6-luna` with `SECURE_QUERY_REASONING_EFFORT=none` (the only
  setting where it accepts a temperature), not adjudicated: Chinook dev 13 wrong of 134
  (9.7%) against 7 (5.2%) for `gpt-4.1-mini`; Northwind dev 4 wrong of 10 against 3. Not
  switched. Still untried: `gpt-6-luna` with reasoning on, and `gpt-6.1-terra`.
