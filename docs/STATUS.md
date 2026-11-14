# Status

**Updated:** 2026-11-13 · Tasks: [PLAN.md](PLAN.md)

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
- Auth modes `dev` / `token` / `header`; production forbids `dev` and refuses to start
  without a 32-character `SECURE_QUERY_REVIEW_SECRET`.
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
2.1–5.8%)**; refused 13.3%. Both raters were models (gpt-5 and Claude); no human rated
this round. Files: `docs/baselines/spider-test400-gpt6luna-gate-r3-2026-11-12.json` and
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
  `/private/tmp/claude-501/-Users-USER-Desktop-secure-query/086bb7dc-9aaf-474c-bcae-4aab5a73fdd1/scratchpad/worktree-backup-20261111-2002.tgz`
  (temporary; copy it somewhere permanent if you want to keep it).
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
