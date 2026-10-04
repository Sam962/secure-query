# Status

**Updated:** 2026-08-18

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

**Holdout (qwen2.5:7b, 2026-10-03): 10/12, 2 wrong — gate failing.** The Ollama tag was
re-pulled 2026-09-07 and the model's plans changed; the prompt did not. The 2026-08-18
result (12/12, 0 wrong) was on the earlier model build. See
[baselines/holdout-2026-10-03.txt](baselines/holdout-2026-10-03.txt). `llama3.2` is not a valid regression test.

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
| Holdout size | ≥ 150 real questions, 5 repeats | 0/12 wrong only bounds wrong-rate below ~25% (rule of three); 0/150 bounds it near 2% |
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
4. [ ] Metric × dimension planning: the model picks
   `{metric, dimensions, filters, time_grain}`; joins come from the catalog.
5. [ ] Eval report: Wilson 95% bounds, per-tag breakdown, over-refusal rate.
6. [ ] Nightly live-model eval job (CI keeps the mock gate). Pin the model by
   digest and record it in every baseline; a tag re-pull silently broke the gate.
7. [ ] Restore holdout wrong-rate = 0 on the current model without touching the
   holdout: reproduce both failure shapes (top-N sorted by a label first;
   per-entity average answered instead of declined) as dev cases, fix there.

### Phase B — real domain (needs a design partner)

8. [ ] Pick one schema + one owner; fill [OWNERSHIP.md](OWNERSHIP.md).
9. [ ] Collect ≥ 150 questions from real sources (Slack, tickets, dashboards).
   Reference SQL written by someone other than the builder. Freeze the holdout.
10. [ ] Draft catalog → owner approves PII, join keys, metrics.
11. [ ] Token or SSO auth; SELECT-only execute identity.

### Phase C — head-to-head

12. [ ] Genie adapter in the eval harness (Conversation API), same questions.
13. [ ] Equal curation: Genie space gets the same instructions, metric
    definitions (as trusted assets), and descriptions. Pin dates and versions.
14. [ ] Publish: correct / wrong / abstain with confidence bounds, both systems.

### Phase D — only after C

15. [ ] Follow-up questions as plan edits (the model edits the prior plan).
16. [ ] Charts chosen deterministically from plan shape.

**Frozen until Phase C:** new execute backends, new LLM providers.

Do **not** build self-serve “paste a Databricks URL and chat.” Unity Catalog is the warehouse lock; Secure Query’s catalog is the planner allowlist.

## Intentional non-goals

- LLM-written SQL, regex “SELECT-only” safety, SQL error-repair loops
- Growing LogicalPlan into a general SQL AST ([ADR 002](adr/002-ir-boundary.md))
- Sending result cells to an LLM ([ADR 001](adr/001-result-policy.md))
