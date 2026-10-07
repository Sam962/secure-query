# Validated SQL as the primary planner path

**Status:** Accepted (rollout behind `SECURE_QUERY_PLANNER=sql`)
**Date:** 2026-10-06
**Supersedes:** [ADR 002](002-ir-boundary.md) in part — the LogicalPlan stays as it is;
it is no longer the only way a question becomes SQL.

## Context

ADR 002 kept the model inside a small JSON plan format (LQP) so that nothing it
wrote could reach the warehouse as SQL. On 400 Spider dev questions over 20
schemas never used for tuning, with the same model (qwen2.5:7b), the same
scoring and the same sample-agreement gate
([STATUS](../STATUS.md#lqp-vs-validated-sql-on-spider-dev--2026-10-06)):

| Planner | Policy | Right | Wrong | Precision |
|---------|--------|-------|-------|-----------|
| LQP | baseline | 27.8% | 15.0% | 64.9% |
| LQP | 3/3 agree | 23.2% | 6.5% | 78.2% |
| Validated SQL | 2/3 agree | 60.2% | 15.0% | 80.1% |
| Validated SQL | 3/3 agree | 53.5% | 9.8% | 84.6% |

The LQP's coverage limit (no nested queries, set operations, arithmetic or
DISTINCT lists) and the model's unfamiliarity with the JSON format cost more
than half of the answerable questions, and the model still approximated
inexpressible questions wrongly (45% of LQP wrong answers).

## Decision

The model may write SQL. **The SQL it writes never runs.** `kernel/sql_validate.py`
parses it, resolves every column against the catalog, enforces policy and
regenerates the SQL that executes:

- exactly one read-only query; catalog tables and CTEs only; no table functions
- every column resolves to an approved catalog column; none is `pii=high`
- joins are equi-joins on approved join keys; no cross joins; no unused joins
- no aggregate over rows a one-to-many join repeats (fan-out)
- only known functions; outer LIMIT capped at the catalog maximum
- principal row filters: the filtered table is wrapped in a filtered subquery and
  every other table is restricted through its approved join path with EXISTS;
  a table with no path to a filtered table is rejected

The LogicalPlan path's semantic guards (dropped concepts, dropped values,
missing average / count) run on what the validated SQL reads. The LQP and
approved metrics remain the path for governed numbers.

This is structural validation of a resolved syntax tree, not the regex
"SELECT-only" filtering ADR 002 rejected.

## Consequences

- `kernel/sql_validate.py` is now security-critical code. Every change needs an
  adversarial test (PII through aliases, subqueries, UNION, CTEs, EXISTS; row
  filters through subqueries, UNION ALL, CTEs and indirect tables).
- The model's text is audited as input only; the audit records the regenerated SQL.
- Wrong-rate is still far above the 2% target with a 7B model; the default stays
  `lqp` until the SQL path, with its guards, is measured on held-out data
  (Spider test) and a stronger model is evaluated.
- Remaining gaps: execution-error repair (type mismatches surface as errors),
  and explanations for SQL answers are generic.
