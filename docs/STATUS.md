# Status

**Updated:** 2026-10-04

## Ready

- Trust kernel: LogicalPlan → validate → AST compile → read-only execute → audit
- Chinook demo catalog (11 tables), DuckDB, confirm-then-run UI
- Planner: Ollama / Groq / OpenAI / mock; default local model **qwen2.5:7b**
- Guards: PII, out-of-scope, useless joins, catalog suggestions (askable only)
- Metrics registry (including ratio AST builders)
- Auth: `dev` / `token` / `header`; `SECURE_QUERY_ENV=production` blocks `dev`
- Execute: DuckDB, Postgres, Databricks SQL warehouse
- Unity Catalog **draft** from `information_schema` (not an approved allowlist)
- Eval splits: 101 dev / 12 holdout; gate is holdout **wrong-rate = 0**
- CI: pytest, f-string SQL grep, mock holdout

**Holdout (qwen2.5:7b @ 845dbda0ea48, 2026-10-04):** 12/12, 0 wrong (3 repeats).
**Dev:** 100/101, 0 wrong. The 2026-10-03 failure was model drift (tag re-pulled
2026-09-07); see [baselines/holdout-2026-10-04.txt](baselines/holdout-2026-10-04.txt).
`llama3.2` is not a valid regression test.

## Not production on company data

- No named domain owner ([OWNERSHIP.md](OWNERSHIP.md) is a template)
- No approved warehouse catalog (Chinook is the demo)
- CI does not run the live model
- Holdout n=12, one repeat — real domains need their own questions
- A valid plan can still be the wrong business number (e.g. MAX vs COUNT)

## Plan: beat Genie on trust, measured

**Goal:** on one real domain, with equal curation effort, Secure Query is wrong
measurably less often than Databricks Genie, at an answer-rate users accept.

**Success bar (fix before building; do not move it after seeing results):**

| Metric | Target | Why |
|--------|--------|-----|
| Holdout size | ≥ 200 real questions, 5 repeats | 95% Wilson upper bound on 0 wrong: 0/12 → 24%, 0/113 → 3.3%, 0/150 → 2.5%, 0/200 → 1.9% |
| Wrong-rate (95% upper bound) | ≤ 2%, and below Genie's | The product metric |
| Answer-rate | ≥ 70% of answerable questions | Refusal-everything must not pass |
| Over-refusal | Reported per guard | A guard that blocks more right answers than wrong ones goes |

### Phase A — kernel fixes (no external dependencies)

1. [x] Commit-ready baseline: refactor into `kernel/ planner/ engine/ auth/ api/`.
2. [x] Metrics are catalog data. `Catalog.metrics` holds analyst-owned
   `MetricSpec`s loaded from JSON; no Chinook registry hardcoded in the kernel.
3. [x] Structured ratio metrics (`numerator` / `denominator`) compile through the
   validated plan path, so principal row filters apply. Code-registered AST
   builders remain only as `kind="builtin"` and stay blocked under row filters.
   - [x] Found while doing this: a principal slice with no join keys between its
     tables fell back to "allow any join". `join_keys` is now a strict allowlist;
     `allow_any_join` is an explicit dev-only flag.
4. [x] Joins come from the catalog, not the model (`kernel/joins.py`). The model
   names columns only; any `joins` it writes are dropped. The kernel connects
   the tables it reads through `join_keys` by shortest path and rejects
   ambiguous paths (`plan.ambiguous_join_path`), unreachable tables and
   fan-out: SUM/AVG/COUNT over rows a one-to-many join repeats (`plan.fan_out`;
   `JoinKey.left` is the many side). FK group keys are rewritten to their
   `label_for` column.
   - [x] `having` targets an aggregate alias (`HavingFilter`).
   - [x] Guards that no longer fit were removed (`useless_joins`, lookup-group
     inference). New deterministic guards, all dataset-agnostic: a named value
     (proper noun, year) with no filter, a filter value the question never
     says (`dropped_filter`), "how many" without a count, an approved metric
     sharing no word with the question, inexpressible operations (percent,
     change, than average …). Chinook nouns removed from the guard stop-word list.
   - [ ] Prompt examples still use Chinook table names; generalise and check
     on a second schema.
5. [x] Eval report: Wilson 95% bounds, answer-rate and over-refusal on answerable
   cases, per-control refusal scorecard, per-tag breakdown, `--json` output.
6. [ ] Nightly live-model eval job (CI keeps the mock gate).
   - [x] Pin the model by digest: `--expect-digest` / `SECURE_QUERY_MODEL_DIGEST`
     refuses to run on another build (a tag re-pull silently broke the gate).
   - [x] Pin the Chinook data to an upstream commit + SHA-256; CI loads it with
     `--strict` (no synthetic fallback).
7. [x] Restore holdout wrong-rate = 0 on the current model. Fixes:
   - `plan.dead_order_keys`: sort keys after the full group key are rejected,
     which sends the model into repair (top-N sorted by label first).
   - `dropped_average` guard: an "average" question answered without AVG is refused.
   - Date literals: ISO strings now validate for `type=date|datetime`; before
     this no date filter from the planner could ever pass. Prompt shows the
     `between` and date-range shapes.
   - Calendar words ("half", months, "quarterly") are not out-of-scope terms.
   - Eval harness: tie-aware top-N scoring; 13 dev cases fixed whose reference
     returned 0 (years outside 2021–2025, "UK" vs "United Kingdom", thresholds
     above the max) and so scored wrong filters as correct.
   - Caveat: the first two rules were designed after seeing holdout failures.
     Dev results are the evidence they generalise.

8. [ ] Complex questions (20 dev cases): answer-rate 35% (6/17) →
   **65% (11/17)**, still 0 wrong; target 70%. Full dev 113/121, 0 wrong
   (95% CI 0–3.1%), answer-rate 93%; holdout 12/12, 0 wrong. qwen2.5:7b @
   845dbda0ea48, 1 repeat. See [baselines/dev-joins-2026-10-04.json](baselines/dev-joins-2026-10-04.json).
   - [x] Keyword out-of-scope guard no longer refuses questions (6 false vs 3
     correct). It still filters catalog suggestions.
   - [x] HAVING on an aggregate alias.
   - [ ] Remaining refusals on complex: the model drops a filter it was given
     (Jazz, AC/DC), groups by the wrong key (artist revenue), or converts units
     ("5 minutes" → 300000 ms, refused as an unmentioned value).
   - Caveat: the inexpressible-operation words were chosen from dev cases; the
     holdout `revenue_growth_rate` also matches them.

### Phase B — real domain (needs a design partner)

9. [ ] Pick one schema + one owner; fill [OWNERSHIP.md](OWNERSHIP.md).
10. [ ] Collect ≥ 200 questions from real sources (Slack, tickets, dashboards).
   Reference SQL written by someone other than the builder. Freeze the holdout.
11. [ ] Draft catalog → owner approves PII, join keys, metrics.
12. [ ] Token or SSO auth; SELECT-only execute identity.

### Phase C — head-to-head

13. [ ] Genie adapter in the eval harness (Conversation API), same questions.
14. [ ] Equal curation: Genie space gets the same instructions, metric
    definitions (as trusted assets), and descriptions. Pin dates and versions.
15. [ ] Publish: correct / wrong / abstain with confidence bounds, both systems.

### Phase D — only after C

16. [ ] Follow-up questions as plan edits (the model edits the prior plan).
17. [ ] Charts chosen deterministically from plan shape.

**Frozen until Phase C:** new execute backends, new LLM providers.

Do **not** build self-serve “paste a Databricks URL and chat.” Unity Catalog is the warehouse lock; Secure Query’s catalog is the planner allowlist.

## Intentional non-goals

- LLM-written SQL, regex “SELECT-only” safety, SQL error-repair loops
- Growing LogicalPlan into a general SQL AST ([ADR 002](adr/002-ir-boundary.md))
- Sending result cells to an LLM ([ADR 001](adr/001-result-policy.md))
