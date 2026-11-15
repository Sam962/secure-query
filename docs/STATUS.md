# Status

**Updated:** 2026-11-15 · Tasks: [PLAN.md](PLAN.md)

## What works (verified in code and tests)

- Trust kernel: model SQL → allowlist validate → regenerate → read-only execute → audit + trace.
- The allowlist refuses COLUMNS(), `#n`, STRUCT(*), whole-row references, VERSION AS OF,
  TABLESAMPLE, fan-out through a CTE, and derived-side Cartesian joins
  (`tests/test_sql_validate.py`, `tests/test_adversarial.py`).
- A trace record for every ask, confirm, refuse and execute (`data/trace.jsonl`); the
  question is stored only as a hash.
- A signed Evidence Bundle on every response, shown in the UI as "Why this was allowed".
  The bundle lists the checks of the path that produced the query (SQL, LogicalPlan or metric).
- Self-consistency gate: the question is planned twice; different answers refuse.
- A ranked or thresholded answer shows the number it was ranked by, whatever the model wrote.
- Correlated subqueries (`WHERE NOT EXISTS (... = outer.id)`) validate; outer references get
  the same catalog and PII checks as local columns.
- Any model on an OpenAI-compatible endpoint: the client drops or swaps a parameter the
  model rejects (`planner/llm.py`).
- Auth modes `dev` / `token` / `header` / `jwt` (OIDC); production forbids `dev` and refuses
  to start without a 32-character `SECURE_QUERY_REVIEW_SECRET` or with an unapproved catalog.
- Signed reviews expire; Evidence Bundles can be Ed25519-signed and verified with a public
  key; trace and audit files are hash-chained; per-person rate limit and `/metrics`.
- Databricks: execution as the end user (`SECURE_QUERY_DATABRICKS_AUTH=user`); the UC
  catalog draft reads real foreign keys; sensitive-looking column names default to high PII.
- Genie Conversation API client and head-to-head scorecard (code only; no live run yet).

## Not production on company data

- No named domain owner ([OWNERSHIP.md](OWNERSHIP.md) is a template).
- No approved warehouse catalog. Chinook is the demo.
- No live Genie numbers. The partner date and workspace are not set (PLAN 3.1).
- The live Databricks smoke has never run against a workspace.

## Latest measured (SQL path)

**Default model: `gpt-6-luna`, reasoning off** (since 2026-11-11). Spider test 400, gate on,
3 repeats (2026-11-12): **76.8% right; adjudicated real wrong 3.5% (14/400, 95% CI
2.1–5.8%)**; refused 13.3%. A rerun on 2026-11-13 after the gate compared whole
answers gave 310 right / 38 wrong / 52 refused by exact match; its two new wrong answers
were rated on 2026-11-14, which makes **15 real errors of 400 (3.75%, 95% CI 2.3–6.1%)** for
that run. Both raters were models (gpt-5 and Claude); no human rated either round. Answers
scored right were audited for the first time on 2026-11-14 (changelog): the raters flag
between 1 and 11 of 60, so the wrong-rate may be understated until a person rates them. Files: `docs/baselines/spider-test400-gpt6luna-gate-r3-2026-11-12.json` and
`docs/adjudication/`. This is above the 2% goal, as the previous model's number was.

Previous default, `gpt-4.1-mini`:

Spider test 400, gate + tie-break (2026-10-11): **78.8% right; adjudicated real wrong
2.75% (11/400, 95% CI 1.5–4.9%)**; flip rate 5.5%. Files: `docs/baselines/` and
`docs/adjudication/`. BIRD is a stress test, not a target.

Table-confidence refusal (PLAN 1.4, 2026-11-11, no LLM): on a 22-table catalog (Chinook +
Northwind) at threshold 0.55 it would refuse **17 of 53 answerable questions (32.1%)**:
Chinook 11 of 25, Northwind 6 of 28. That is above the 5% rule, so the gate is **off by
default** (`SECURE_QUERY_LINK_CONFIDENCE=0`). It has its own refusal code,
`low_link_confidence`. Whether it prevents wrong answers is still unmeasured.

DPC-lite (PLAN 1.5, 2026-11-11, gpt-4.1-mini, SQL path with the gate): **0 `unstable_probe`
refusals** on Chinook dev (134 cases) and Northwind dev (10 cases). It never changed an
outcome, so `planner/dpc_lite.py` was deleted; gate fingerprints now come from the
two-readings check only. Same runs, not adjudicated: Chinook dev 119/134 right, 7 wrong
(5%), 7 abstained, 1 harness error; Northwind dev 7/10 right, 3 wrong.

## Changelog

- **2026-11-15 — head-to-head on three Spider test databases on Databricks, 237 questions:
  Secure Query gives fewer wrong answers and far fewer answers.** Databases `aan_1`,
  `bakery_1`, `advertising_agencies`; 3 repeats; `gpt-6-luna` with the gate. The raters are
  two models (gpt-5 and the assistant), no person; the assistant could tell the systems
  apart by SQL style, so its rating is not blind.

  | 237 questions | Secure Query | Genie |
  |---|---|---|
  | Right by exact match | 165 | 197 |
  | Wrong by exact match | 24 | 40 |
  | Refused | 48 | 0 |
  | Answer differs between repeats | 12 | 30 |
  | Real errors, either rater | 9 (3.8%, CI 2.0–7.1%) | 18 (7.6%, CI 4.9–11.7%) |
  | Real errors, gpt-5 alone | 9 | 16 |
  | Real errors, assistant alone | 5 | 11 |

  - The intervals overlap; this is not a win. Of answers given, 9 of 189 (4.8%) against
    18 of 237 (7.6%). The 2% bar is missed.
  - Per database (right / wrong / refused; real errors either rater): `aan_1` 56 questions,
    47/6/3 and 4 against 46/10/0 and 5; `bakery_1` 94, 48/8/38 and 3 against 78/16/0 and
    8; `advertising_agencies` 87, 70/10/7 and 2 against 73/14/0 and 5.
  - 31 of the 38 `bakery_1` refusals are one catalog gap: the Spider schema has no foreign
    key from `items.Receipt` to `receipts.ReceiptNumber`, so the catalog has no such join
    and Secure Query refuses it. Genie got the same two joins and joined anyway, mostly
    correctly. A catalog owner would approve that join; the run was not repeated with it.
  - Suites now leave out what exact match cannot grade (P10, `scripts/spider_warehouse_suite.py`):
    an answer over 5,000 rows, a gold query that selects a column its GROUP BY does not
    determine, a gold LIMIT that cuts through a tie. `aan_1` went from 68 to 56 questions;
    its numbers above are the 2026-11-14 answers re-scored on the 56, not a new run.
  - The `bakery_1` and `advertising_agencies` tables and Genie spaces were made by
    `scripts/spider_warehouse_load.py`: tables, column descriptions and joins from the
    Secure Query catalog, no instructions, no example questions. The `aan_1` space was made
    by hand and has a one-line instruction.
  - Real errors seen. Secure Query: a filter on the wrong column or a misspelt value that
    returns nothing (3 on `bakery_1`); a dropped requested column. Genie: date parsing that
    drops single-digit days; `COUNT(DISTINCT type)` for "how many cakes"; a join that
    multiplies rows before `HAVING COUNT = 1`.
  - **Re-run of `bakery_1` with that join approved for both systems** (added to the catalog
    copy and to the Genie space, same day): Secure Query 70 right / 11 wrong / 13 refused,
    Genie 79 / 15 / 0; real errors by either rater 4 against 4. With this run in place of
    the first, the 237 questions give: right 187 against 198, wrong 27 against 39, refused
    23 against 0, answers differing between repeats 10 against 29, real errors by either
    rater 10 (4.2%, CI 2.3–7.6%) against 14 (5.9%, CI 3.6–9.7%); gpt-5 alone 10 against 14,
    the assistant alone 5 against 7. Genie's real errors on `bakery_1` fell from 8 to 4
    between the two runs, so run-to-run variation is as large as the gap between systems.
    Files: `*-bakery1-join-2026-11-15*`.
  - Files: `docs/baselines/genie-h2h-spider-{bakery1,advertising}-2026-11-15.json`,
    `docs/adjudication/genie-h2h-spider-{bakery1,advertising}-2026-11-15-*`.
- **2026-11-14 — first head-to-head on a set we did not develop on: Spider test `aan_1` on
  Databricks. No clear winner.** 242,907 rows in `workspace.aan_1`; a Genie space with the
  5 tables, 5 joins and a one-line description, no example questions. 68 questions (of
  Spider's 90: 20 have an empty or zero gold answer and are skipped by the loader, 2 have a
  gold query with no single answer), 3 repeats, `gpt-6-luna`, everything read from the
  warehouse. Accuracy only: Spider has no should-refuse cases.

  | | Secure Query | Genie |
  |---|---|---|
  | Right by exact match | 49 | 46 |
  | Wrong by exact match | 16 | 22 |
  | Refused | 3 | 0 |
  | Answer differed between repeats | 8 | 13 |
  | Real errors, either rater says so | 8 (11.8%, CI 6.1–21.5%) | 11 (16.2%, CI 9.3–26.7%) |
  | Real errors, gpt-5 alone | 8 | 11 |
  | Real errors, Claude alone | 2 | 2 |

  - **The intervals overlap; this does not show Secure Query is wrong less often.** 68
    questions is a third of the plan's minimum, and no person rated anything.
  - **The raters disagree widely** (same label on 16 of 38, error-or-not on 23 of 38).
    Both saw shuffled packets with no system name. Most of the gap is two things gpt-5
    could not see from a packet: author names are unique in this data, so grouping by name
    is not an error; and a count that differs only because the result was cut at a row cap.
  - **The row caps make 14 of the 38 packets ungradable.** The reference and Secure Query
    stop at 10,000 rows, the Genie API returns at most 5,000, and several questions here
    have larger answers. Exact match then compares two samples (PLAN P10).
  - **8 of the 38 are the benchmark's fault:** for "the most cited paper" the gold returns
    the id of an arbitrary citing paper. Both systems answer with the cited paper.
  - Real errors both raters accept. Secure Query: `name = 'Stanford'` for the stored
    'Stanford University' (an empty answer); needless inner joins that drop the 1,174
    authors with no paper. Genie: grouping by title, which adds up the citations of
    different papers that share one (twice).
  - Secure Query answered from the SELECT below 10,000 rows with a silent cut; so did
    Genie at 5,000. Neither tells the user the answer is incomplete in this harness.
  - Fixed, both in the grader: when the gate refused an answer on the warehouse the
    blocked SQL was parsed as DuckDB and the attempt was recorded as a harness error (6
    questions in the first run; Secure Query was re-run after the fix, Genie was not).
    Tests no longer inherit the developer's `.env`: an entrypoint under test loaded it and
    switched later tests to the warehouse.
  - New: `scripts/spider_warehouse_suite.py DB DIR` converts a Spider database's gold SQL
    to Databricks and keeps a question only when it returns the gold's rows on the
    warehouse.
  - Files: `docs/baselines/genie-h2h-spider-aan1-2026-11-14.json`,
    `docs/adjudication/genie-h2h-spider-aan1-2026-11-14-*`.

- **2026-11-14 — first live run against Databricks and Genie (a trial, not a result).**
  Sam's workspace: the 11 Chinook tables in `workspace.default` on a serverless warehouse,
  and a Genie space over them with instructions, 3 measures, 10 joins and 2 example
  queries. Chinook is the set we developed on, so nothing here may be quoted as a
  comparison. `chinook/cases.json` (25 answerable, 9 should-refuse), 3 repeats,
  `gpt-6-luna`, everything read from the warehouse.
  - Before the run: all 25 reference queries return the same rows on the warehouse as on
    the local database; Genie's 2 example questions are not in the suite.
  - Exact-match scorecard: Secure Query 1 wrong of 34, Genie 10 of 34. Read case by case
    by Claude (a model, not blind, not a person), almost none of that is accuracy:
    - **Answerable (25): both systems answered all 25 correctly.** Genie's 3 "wrong" were a
      timestamp with a UTC marker that the grader did not equate (fixed, below), the
      right value with the invoice id next to it, and the right 5 customers with other
      columns.
    - **Personal data (5 should-refuse): Secure Query refused all 5; Genie returned
      e-mails, a phone number and addresses in all 5,** and an e-mail column in the
      Brazil customer list. The Genie space was given no rule about these columns, so
      this measures configuration as much as the product.
    - **Two stale expectations.** `avg_revenue_per_customer` and `revenue_growth_rate` are
      marked should-refuse because the old plan format could not express a ratio. SQL can.
      Secure Query answered the first (scored wrong, but its SQL is the formula the case
      asks for) and refused the second; Genie answered both. The second is an
      over-refusal by Secure Query. The suite labels need Sam's decision.
    - Genie gave a differently shaped answer between repeats on 3 of 34 questions;
      Secure Query on none.
  - Fixed: a Genie timestamp arrives marked UTC and never equalled the same instant from
    the SQL driver, so every date-bucket answer from Genie was scored wrong.
  - Ran live for the first time: `--backend databricks`, the Genie client, execution on
    a warehouse with connection reuse. Still not run live: the catalog draft from Unity
    Catalog, the drift check, `scripts/databricks_smoke.py`, OIDC, the Docker image.
  - Not changed in the workspace or `.env`. Three tables are snake_case there
    (`invoice_line`, `media_type`, `playlist_track`); the run used a temporary copy of the
    catalog and suite with those names. `.env` lacks `DATABRICKS_HTTP_PATH` and
    `SECURE_QUERY_DATABRICKS_SCHEMA`; they were passed at run time.
  - File: `docs/baselines/genie-h2h-chinook-trial-2026-11-14.json`.

- **2026-11-14 — audit of answers scored right; a different model as second reader.**
  Prompted by two papers (SpotIt, arXiv 2510.26840; a selective-prediction study, arXiv
  2607.06799). VeriPIE (arXiv 2610.17493) was read and nothing was taken from it.
  - **The two unlabelled wrong answers of the 2026-11-13 run are rated.** gpt-5 and Claude
    agree, each without seeing the other: `test_1041_aan_1` is a real error,
    `test_0453_cre_Students_Information_Systems` is ambiguous. That run has 15 real errors
    of 400 (3.75%, CI 2.3–6.1%). No human rated them.
  - **Audit of right answers.** A result that matches the reference on one database can
    come from a query that is wrong on other data; SpotIt measured 10 to 13 points of this
    on BIRD. `scripts/adjudicate.py packets REPORT --right N` samples matching answers and
    `rate` judges the query itself. On 60 of the 310 right answers of the 2026-11-13 run:
    gpt-5 flags 11; Claude, reviewing gpt-5's first-pass flags (not blind), accepts 1
    (`test_1168_pilot_1`: `ORDER BY age LIMIT 1` where the reference keeps every row at the
    minimum). The other 10 need data the schema's keys rule out or share the weakness with
    the reference. The raters disagree too much for a number: between 1 of 60 (1.7%, CI
    0.3–8.9%) and 11 of 60 (18%, CI 10–30%). A person has to rate these 60. gpt-5's first
    pass flagged 40, of which 25 only for the 10,000-row cap; the rubric now excludes the
    cap, single-row ties and zero-count groups. Files: `docs/adjudication/*-right60-*`.
  - **Lead for the warehouse, not fixed:** about half of the 11 flags are about NULL. One matters on
    Databricks: it sorts NULL first ascending, DuckDB sorts it last, so "the lowest X"
    written as `ORDER BY x LIMIT 1` returns a NULL row there when the column has one. No
    eval here can see it (PLAN P9).
  - **Second reader as a different model (`SECURE_QUERY_CHECK_MODEL`, off by default).**
    Spider **dev** 400, 3 repeats, exact match, not adjudicated, same day:

    | Second reader | Right | Wrong | Refused | Answer-rate |
    |---|---|---|---|---|
    | `gpt-6-luna` (default) | 300 | 42 | 58 | 75.0% |
    | `gpt-6.1-terra` | 281 | 29 | 90 | 70.3% |

    14 of the 42 wrong answers became refusals, and so did 27 of the 300 right ones; 8
    refusals became right and 1 became wrong. Wrong answers fall by 31% and right answers
    by 6%; "unstable answer" refusals go from 28 to 62. The setting is one variable, read by
    the product and both eval runners, on the same endpoint, so nothing new is sent
    anywhere. Not confirmed on Spider test and not adjudicated; the default is unchanged
    (PLAN P8). Files: `docs/baselines/spider-dev400-gpt6luna-*-2026-11-14.json`.
  - The eval runners now take the second reader from the client (`check_twin`), as the
    product does. Mock runs are unchanged; 654 tests pass.

- **2026-11-14 — head-to-head harness ready (PLAN 2.1, 2.2).** `evals.scorecard --backend
  databricks` runs reference SQL, Secure Query and its lookups on the warehouse (needs
  `SECURE_QUERY_SMOKE_WAREHOUSE_TOKEN`, SELECT-only); the default DuckDB scorecard output
  is unchanged. `--out` now writes every case of both systems, and
  `scripts/adjudicate.py packets SCORECARD.json` builds shuffled, numbered packets with no
  system name, plus a separate key file. Checked live on Northwind dev (10 questions, one
  wrong, one packet). The Databricks backend was tested with a fake runner, not a warehouse.
- **2026-11-13 — production hardening (PLAN Phase 4) and one serious fix.**
  - **The consistency gate was off for every API and CLI request** (PLAN 4.12). `ask()`
    skipped the second reading whenever a client was passed in, and both entry points pass
    one. Only the eval harness ran the gate, so every measured number describes a
    configuration the product did not run. Fixed; a live request now runs two readings
    (three audited warehouse statements for one confirm-and-run).
  - The gate now compares whole answers, not the first 50 rows (4.8). Spider test 400, 3
    repeats, `gpt-6-luna`, exact match: **310 right / 38 wrong / 52 refused** (was 307 / 40
    / 53). Not re-adjudicated: of the 38, 36 are questions labelled on 2026-11-12 (14 real
    errors, 12 ambiguous, 10 benchmark faults, matched by question id) and 2 are new and
    unlabelled, so real errors are between 14 and 16 of 400. All 14 earlier real errors are
    still answered wrong. File: `docs/baselines/spider-test400-gpt6luna-gate-r3-2026-11-13.json`.
  - Added: review expiry and secret rotation (4.1); Ed25519 Evidence Bundles with a public
    key endpoint (4.2); OIDC sign-in with group mapping (4.3); principals file reload (4.4);
    hash-chained, lock-safe trace and audit files with a verify script (4.5); per-person
    rate limit, `/metrics`, request ids, model-provider breaker (4.6); Databricks
    connection reuse per credential (4.7); catalog approval record and drift check (4.9);
    production Docker image (4.10); what is sent to the model provider (4.11).
  - Checked live through the API with `gpt-6-luna` on DuckDB: token sign-in, confirm, run,
    forged expiry refused, evidence verified with the public key alone, refusal, metrics,
    both audit files intact.
  - **Not verified:** the Docker image was not built (no Docker on this machine); OIDC was
    tested with locally generated keys, not a real identity provider; Databricks connection
    reuse and the `--databricks` drift check were tested with fakes, not a warehouse.
  - Not done, by decision: retiring the LogicalPlan planner (it is the only route to
    metrics; PLAN P1), certified answers (P4), masking (P5), conversation (P6).
- **2026-11-13 — the two readings run at the same time (PLAN 1.6 done).** Median seconds
  per question, 20 Chinook dev questions, `gpt-6-luna`, planning only: **2.06 s before,
  0.87 s after**. The comparison logic is unchanged. Cost: when the first reading refuses,
  the second call has already been sent, so a refused question now costs two LLM calls
  where it cost one.
  - Chinook dev, one run each, right / wrong / refused: sequential 128 / 4 / 2; parallel
    126 / 4 / 4 and 125 / 6 / 3. The runs differ on the same four questions. Planned 16
    times each, all four flip between answer and refusal in both versions, so this is the
    known run-to-run noise of the gate, not the change. One of them
    (`dev_complex_rock_revenue_share`) was refused as unstable 7 of 16 times sequential
    and 1 of 16 parallel; with four questions compared that can be chance, and it is not
    explained.
- **2026-11-13 — review of the 2026-11-11/12 changes (offline; no live runs).**
  - Measure column and the suites: no reference in the Chinook or Northwind suites, dev or
    holdout, ranks or filters by an aggregate it does not show, so the added column cannot
    turn a right answer wrong there. Checked by applying the same rewrite to every
    reference; none changed.
  - Tie rule audit on Spider test 400: 9 answers per run pass only through the tie rule; 7
    are truly tied on the ranking value. The other 2 are the same two questions in every
    run back to 2026-10-11, and in both the benchmark sorts a number stored as text, so the
    system's answer is the right one. The rule's known gap hid no real error.
  - Prompt versus validator, Spider dev 400, right / wrong / refused, one run each:

    | | `gpt-6-luna` | `gpt-4.1-mini` |
    |---|---|---|
    | Before both | 278 / 30 / 92 | 306 / 30 / 64 |
    | Prompt change only | 291 / 31 / 78 | 309 / 33 / 58 |
    | Prompt + validator fix | 311 / 36 / 53 | 309 / 33 / 58 |

    The prompt change added 13 right answers and 1 wrong for `gpt-6-luna`; the validator
    fix added 20 right and 5 wrong, and did not move `gpt-4.1-mini`.
  - Both rewrites checked on the Databricks and Postgres dialects. The client's own
    reasoning default no longer logs a warning when a model does not know the setting.
- **2026-11-12 — `gpt-6-luna` adjudicated on Spider test 400 (PLAN 1.7 done).** 40 wrong by
  exact match: 14 real errors (3.5%, CI 2.1–5.8%), 15 ambiguous, 11 benchmark faults. 13
  labels reused from earlier rounds (same rows); 27 rated by gpt-5 and a second model
  rater (Claude), merged as real error if either said so. See `docs/adjudication/README.md`.
- **2026-11-12 — `gpt-6-luna` now level with `gpt-4.1-mini` on Spider test (PLAN 1.7).** Two
  fixes, both found on Spider **dev** and confirmed once on test:
  - Validator bug: every correlated subquery (`WHERE NOT EXISTS (... = outer.id)`) was
    refused as an unknown column. Outer references are now resolved through the enclosing
    queries and get the same catalog and PII checks as local columns.
  - Prompt: the refusal rule now says when to refuse (the catalog truly lacks the table,
    column or relationship) and that wording or a text-typed number is not a reason.

  Spider test, same 400 questions, gate on, 3 repeats, exact match, **not adjudicated**
  (files: `docs/baselines/spider-test400-*-2026-11-12.json`):

  | Model | Right | Wrong | Refused | Answer-rate |
  |-------|-------|-------|---------|-------------|
  | `gpt-6-luna`, before (2026-11-11) | 269 | 29 | 102 | 67.3% |
  | `gpt-6-luna`, after | 307 | 40 | 53 | 76.8% |
  | `gpt-4.1-mini`, before (2026-11-11) | 306 | 33 | 61 | 76.5% |
  | `gpt-4.1-mini`, after | 310 | 39 | 51 | 77.5% |

  Dev suites after the fixes, Chinook dev (134) / Northwind dev (10), right-wrong-refused:
  `gpt-6-luna` 128-5-1 / 9-1-0; `gpt-4.1-mini` 121-5-8 / 8-1-1. Both models answer more and
  are scored wrong slightly more often (exact match); the wrong answers need adjudication
  before any wrong-rate is quoted.
- **2026-11-11 — Spider test 400, successor models against `gpt-4.1-mini` (PLAN 1.7).** Same
  400 questions, SQL path, gate on, 3 repeats, exact match, **not adjudicated**. Rescored
  after a grader fix (below). Files: `docs/baselines/spider-test400-*-2026-11-11.json`.

  | Model | Right | Wrong | Refused | Answer-rate |
  |-------|-------|-------|---------|-------------|
  | `gpt-4.1-mini`, 2026-10-11 baseline | 315 | 33 | 52 | 78.8% |
  | `gpt-4.1-mini`, today | 306 | 33 | 61 | 76.5% |
  | `gpt-6-luna`, reasoning off (current default) | 269 | 29 | 102 | 67.3% |
  | `gpt-6-luna`, reasoning low | 271 | 37 | 92 | 67.8% |
  | `gpt-6.1-terra`, reasoning off | 290 | 42 | 68 | 72.5% |

  `gpt-6-luna` is not wrong more often than `gpt-4.1-mini` but refuses far more (66 model
  refusals against 29, mostly "the catalog does not define ..." on loosely named schemas),
  which puts its answer-rate under the 70% bar. The dev suites did not show this.
  - Grader fix: the tie rule (a LIMIT that cuts through a tie may return any tied row) did
    not apply when the answer carried an extra column, so the new measure column turned 5
    to 7 tied answers per run into "wrong". It now applies under `extra_columns_ok`.
  - Checked offline: `show_ranking_measure` rewrote 77 of 734 past Spider queries and none
    failed validation afterwards.
  - Known weakness, not changed: when the reference returns a name only, the tie rule
    cannot tell a tied row from a lower-ranked one.
- **2026-11-11 — ranked answers show their measure; successor models re-measured (PLAN 1.7).**
  The pipeline now adds the aggregate a grouped query sorts or filters by when the model
  left it out (`planner/sql_values.py`, `show_ranking_measure`); it is deterministic and
  independent of model and prompt. The grader accepts an integer year for a year bucket.
  Wrong answers before → after, Chinook dev (134) / Northwind dev (10), one run each, not
  adjudicated:

  | Model | Before | After |
  |-------|--------|-------|
  | `gpt-4.1-mini` | 7 / 3 | 6 / 1 |
  | `gpt-6-luna`, reasoning off | 13 / 4 | 5 / 1 |
  | `gpt-6-luna`, reasoning on | 13 / 4 | 3 / 1 (plus 1 empty reply) |
  | `gpt-6.1-terra`, reasoning off | 11 / 4 | 5 / 2 |

  Every candidate now has no more wrong answers than `gpt-4.1-mini`.
- **2026-11-11 — default model is `gpt-6-luna`, reasoning off.** Sam's decision on the
  table above. The Spider run and adjudication for it are still owed (PLAN 1.7 step 3).
- **2026-11-11 — model-agnostic client; successor models first look (PLAN 1.7).** The
  client drops or swaps a parameter a model rejects, so any model on an OpenAI-compatible
  endpoint runs. `gpt-4.1-mini` shuts down 2027-01-14. Wrong answers on Chinook dev (134) /
  Northwind dev (10), not adjudicated: `gpt-4.1-mini` 7 / 3; `gpt-6.1-terra` (reasoning
  off) 11 / 4; `gpt-6-luna` 13 / 4 with reasoning off and with it on. No model switched.
- **2026-11-11 — DPC-lite measured and deleted (PLAN 1.5).** Live `gpt-4.1-mini` runs on
  Chinook dev (134 cases) and Northwind dev (10 cases): 0 refusals with code
  `unstable_probe`. Removed `planner/dpc_lite.py`, its call and its test; each question
  now costs 2–3 fewer warehouse queries.
- **2026-11-11 — table-confidence gate measured and turned off by default (PLAN 1.4).**
  17 of 53 answerable questions (32.1%) fell below the 0.55 threshold on the combined
  22-table Chinook + Northwind catalog (Chinook 11/25, Northwind 6/28). Default threshold
  is now 0; the refusal code is `low_link_confidence`.
- **2026-11-11 — cleanup and plan rewrite.** Backup of the full pre-cleanup tree:
  `data/backups/worktree-backup-20261111-2002.tgz` (local only; `data/` is not committed).
  - Fixed: the Genie scorecard graded Genie more strictly than Secure Query (no float,
    column-order or tie tolerance), scored a Genie clarifying reply as wrong, and compared
    Genie's string-typed cells with numbers. Genie now uses the same matcher, typed cells,
    and clarifying replies count as refusals.
  - Fixed: the scorecard scored Secure Query without its self-consistency gate, so it
    measured a different system than users get. `evals.run` now defaults to the SQL path,
    matching the product; CI names each path explicitly.
  - Faster: the self-consistency gate ran each reading's query twice. It now runs once.
  - Fixed: the Databricks smoke built its default query with double quotes, which
    Databricks reads as a string, and recorded the row count as the result fingerprint.
  - Reuse: one row-fingerprint function in `kernel/trace.py` serves the trace, the gate
    and the graders. Hash values are unchanged.
  - Removed: the unused Jev linker hook, `engine/federation.py` (only its own test used
    it), `docs/REVIEW-2026-10-06.md` and `docs/BACKLOG.md` (finished; in git history),
    and the stale `.wt-run` worktree (clean, commit `096dc24`).
  - Reuse: the scorecard loads suites with the same function as `evals.run`. Before, a
    Northwind suite fell back to the Chinook catalog; `--suite chinook` also works now.
  - Lint: seven import-order and unused-import errors from the previous session fixed.
  - Tests: 615 passed (three removed with the federation module; five added).
