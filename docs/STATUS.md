# Status

**Updated:** 2026-11-11 · Tasks: [PLAN.md](PLAN.md)

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
- Auth modes `dev` / `token` / `header`; production forbids `dev`.
- Databricks: execution as the end user (`SECURE_QUERY_DATABRICKS_AUTH=user`); the UC
  catalog draft reads real foreign keys; sensitive-looking column names default to high PII.
- Genie Conversation API client and head-to-head scorecard (code only; no live run yet).

## Not production on company data

- No named domain owner ([OWNERSHIP.md](OWNERSHIP.md) is a template).
- No approved warehouse catalog. Chinook is the demo.
- No live Genie numbers. The partner date and workspace are not set (PLAN 3.1).
- The live Databricks smoke has never run against a workspace.

## Latest measured (SQL path, gpt-4.1-mini)

Spider test 400, gate + tie-break (2026-10-11): **78.8% right; adjudicated real wrong
2.75% (11/400, 95% CI 1.5–4.9%)**; flip rate 5.5%. Files: `docs/baselines/` and
`docs/adjudication/`. BIRD is a stress test, not a target.

Unmeasured gates (PLAN 1.4 and 1.5): the table-confidence refusal and DPC-lite.
Forced on for Chinook, the table-confidence gate refused 4 of 25 answerable questions.

## Changelog

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
