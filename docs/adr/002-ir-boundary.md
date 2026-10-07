# IR boundary — what LogicalPlan will never do

**Status:** Superseded in part by [ADR 003](003-validated-sql-planner.md) (2026-10-06)  
**Date:** 2026-08-18

## Decision

The Logical Query Plan (LQP) will **not** grow into a general SQL AST. Questions
that need the following are answered by an **approved metric** or handed to an
analyst (`clarify_code=analyst_handoff`):

- Arbitrary arithmetic between aggregates (ratios, shares, growth rates)
- `UnitPrice * Quantity` style expressions (except as a registered AST metric)
- Subqueries, CTEs, window functions, UNIONs
- Write/admin statements of any kind

## Rationale

Widening the IR until it can express every warehouse query is how this system
becomes “the LLM writes SQL again.” Named metrics keep definitions catalog-owned.
An explicit analyst handoff is a better product than a confident wrong number.

## Consequences

- Planner prompts must refuse inexpressible questions rather than approximate.
- New coverage lands in `kernel/metrics.py` (whitelisted sqlglot builders) or the
  catalog, not as free-form expressions in model output.
