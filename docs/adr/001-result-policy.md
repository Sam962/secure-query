# Result policy — default: no LLM on query result cells

**Status:** Accepted  
**Date:** 2026-08-11

## Decision

Default answer path uses **template formatting** (`secure_query/respond.py`):

- Deterministic explain-back from `explain_plan()`
- Tabular preview of result rows (capped)
- **No LLM** sees result cells in the default path

## Optional (future)

A separate `summarize.py` may call an LLM on **aggregate-only** results with PII column block — not enabled by default.

## Rationale

Sending warehouse rows to an LLM is a second data-exfil path. Template answers preserve the security story: LLM plans only; code executes and formats.
