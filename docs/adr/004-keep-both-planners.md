# Keep both planner paths (LQP and validated SQL)

**Status:** Superseded by [ADR 005](005-allowlist-sql-validator.md)  
**Date:** 2026-10-07 (superseded 2026-11-11)

## Context

Backlog S10 asked whether to retire the Logical Query Plan (LQP) now that the
validated-SQL planner exists. The SQL path is stronger on Spider coverage.

## Original decision

Keep both paths; default `lqp`.

## Supersession

Validated SQL is now the **default production NL path**, after the allowlist
rewrite closed PII and fan-out holes. LQP remains only for catalog-owned metrics
and explain helpers. See ADR 005.
