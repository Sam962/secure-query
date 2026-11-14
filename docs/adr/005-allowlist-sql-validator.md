# Allowlist SQL validator (production NL path)

**Status:** Accepted  
**Date:** 2026-11-11  
**Supersedes:** [ADR 004](004-keep-both-planners.md) for the *default* NL path —
LQP remains for catalog-owned metrics only. Complements [ADR 003](003-validated-sql-planner.md).

## Context

ADR 003 allowed the model to write SQL that is parsed, checked, and regenerated.
A 2026-11-11 adversarial review showed the checker was a **blocklist**: shapes that
read columns without becoming `Column` nodes (`COLUMNS('Email')`, `#12`,
`STRUCT(*)`, Postgres whole-row `SELECT c`, Databricks `VERSION AS OF`) reached
execute and leaked PII. Fan-out aggregates laundered through a CTE, and
`ON b.k = b.k` on a derived side, also passed.

## Decision

1. **Allowlist policy** in `kernel/sql_validate.py`: every projected / filtered /
   ordered expression must resolve to an approved catalog column (or a pure
   function of those). Forbidden AST shapes are rejected by name.
2. **Validated SQL is the default** NL planner (`SECURE_QUERY_PLANNER=sql`).
3. **LQP** stays as the IR for analyst-owned metrics and deterministic explain-back,
   not as a peer NL path.
4. Every ask writes a **TraceRecord** (including refusals), separate from execute audit.

## Consequences

- Adversarial tests in `tests/test_sql_validate.py` and `tests/test_adversarial.py`
  are the regression gate for new sqlglot dialects / node types.
- Product copy must say “model SQL is validated and regenerated,” not
  “the LLM never writes SQL.”
- Evidence Bundle builds on TraceRecord + review HMAC; DPC-lite and link
  confidence thresholds refuse unstable / low-confidence plans.

## Update 2026-11-11

Both gates in the last bullet were measured (PLAN 1.4 and 1.5). DPC-lite never changed an
outcome and was deleted. The link-confidence refusal would have refused 32% of answerable
questions and is off by default. See [STATUS.md](../STATUS.md).
