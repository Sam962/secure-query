# Keep both planner paths (LQP and validated SQL)

**Status:** Accepted  
**Date:** 2026-10-07  
**Supersedes:** nothing. Complements [ADR 003](003-validated-sql-planner.md).

## Context

Backlog S10 asked whether to retire the Logical Query Plan (LQP) now that the
validated-SQL planner exists. The SQL path is stronger on Spider coverage. The
product UI already cannot claim "the model never writes SQL" when
`SECURE_QUERY_PLANNER=sql`. Dropping LQP would delete the IR, the 777-line
validator, and `explain.py`.

## Decision

**Keep both paths.** Selection stays `SECURE_QUERY_PLANNER` (`lqp` default, or
`sql`). Do not retire LQP.

- LQP remains the path for governed numbers, confirm/explain-back, and
  catalog-owned metrics. `explain_plan` is a product surface, not a leftover.
- Validated SQL remains the coverage path. Model SQL never executes; the kernel
  regenerates it after catalog/policy checks (ADR 003).
- Semantic guards that still apply (dropped literals, restricted PII, …) run on
  whichever path produced a plan or a validated read set.

## Consequences

- Two compile/validate stacks stay in the tree. Changes that affect grain,
  joins, or limits should share helpers (see S7–S9) rather than diverge.
- Default stays `lqp` until the SQL path meets the holdout wrong-rate gate on
  the product model. CI already runs both mock holdouts.
- A later ADR can retire LQP only after explain-back has an equivalent on the
  SQL path and the default has switched.
