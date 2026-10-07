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

**Holdout (qwen2.5:7b @ 845dbda0ea48, 2026-10-04):** 12/12, 0 wrong (3 repeats) with
the old Chinook-worded prompt; 11/12 with the generic prompt alone; **12/12, 0 wrong**
with the generic prompt + `averaged_per_other_entity` guard (1 repeat; see item 4).
**Northwind (second schema):** 23/36, 2 wrong (was 4 at first run).
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
   - [x] Prompt examples use a made-up Booking/Hotel schema, not Chinook.
     Found doing this: the old prompt's ratio example was the holdout question
     `avg_revenue_per_customer`, worded almost verbatim. With it removed the
     holdout goes 12/12 → **11/12, 1 wrong** (that case) and dev 113 → 105/121,
     1 wrong. The 0-wrong gate partly measured prompt leakage.
     See [baselines/chinook-generic-prompt-2026-10-04.json](baselines/chinook-generic-prompt-2026-10-04.json).
   - [x] Second schema: Northwind (pinned commit + SHA-256), catalog as JSON
     data only, 36 cases frozen before the run. First live run:
     **22/36, 4 wrong (95% CI 4.4–25.3%)**, answer-rate 54% (15/28).
     See [baselines/northwind-2026-10-04.json](baselines/northwind-2026-10-04.json).
     Do not tune on these cases; reproduce each failure mode in new dev cases first:
     - Wrong entity counted: "how many products do we sell" → COUNT(*) on order lines.
     - Wrong metric picked: "highest freight on a single order" → `freight_by_shipper`,
       and a grouped metric with LIMIT 1 and no ORDER BY returns an arbitrary group.
     - Added filter the question never says (`ShippedDate IS NOT NULL` from "shipped to").
     - Revenue as SUM(UnitPrice) even though the catalog says revenue needs arithmetic.
     - Over-refusal: `dropped_concepts` on common words ("units", "shipped") 5×;
       `dropped_filter` on category/shipper names 3×.
   - [x] Scorecard fix: `dropped_concepts` refusals were labelled `planner_refusal`.
   - [x] `averaged_per_other_entity` guard: "average X per Y" where Y names a
     table and the AVG runs over another table's rows is refused (a ratio the IR
     cannot compute), grouped or not; the message suggests "for each Y". Built
     on 7 new dev cases (`--only avg-per`: 4 wrong → 0 wrong). Results:
     holdout **12/12, 0 wrong** again (the guard, not the prompt, now declines
     `avg_revenue_per_customer`); dev 110/128, 1 wrong (`dev_complex_acdc_album_most_tracks`,
     pre-existing); Northwind unchanged at 22/36, 4 wrong (none were avg-per).
     Known cost: "average track length per genre" is refused.
     See [baselines/chinook-avg-per-guard-2026-10-04.json](baselines/chinook-avg-per-guard-2026-10-04.json).
   - [x] `dropped_concept` looked like a removal candidate (0 correct / 12 false
     on Chinook). Ablation (guard off): Chinook wrong 1 → 13, since all 12 blocked
     plans were wrong; Northwind wrong 4 → 7 (3 wrong + 1 PII leak stopped, 3 right
     answers cost). Kept. The scorecard now runs each guard-blocked plan and
     reports `stopped-wrong` / `cost-right`, so this needs no ablation next time.
     Scored run (no product change): Chinook `dropped_concept` stopped-wrong 12 /
     cost-right 0; Northwind 3 / 3. See [baselines/chinook-guard-scorecard-2026-10-04.json](baselines/chinook-guard-scorecard-2026-10-04.json).
   - [x] Northwind wrong answers 4 → **2** (22 → 23/36, 95% CI 1.5–18.1%):
     kernel rule `plan.arbitrary_group` (LIMIT 1 over groups with no ORDER BY)
     sends `max_freight` to repair, now correct; guard `counted_other_entity`
     ("how many X" must count X's rows unless another question word names the
     source through the catalog) refuses `product_count`. Chinook holdout 12/12,
     0 wrong; no existing case changed verdict. 14 new dev cases (`--suite
     northwind_dev`, `--only unordered-limit,count-entity`); the model got these
     right unaided, so the rules are backstops there. Not caught: counting a
     bridge table whose name contains the noun (EmployeeTerritories for
     "territories"). See [baselines/northwind-count-limit-2026-10-04.json](baselines/northwind-count-limit-2026-10-04.json).
   - [ ] New failure mode (dev): "Which X has the most/fewest …?" answered with a
     ranked top-10 list instead of the single row (2 dev cases).
   - [ ] Remaining Northwind wrong: an added filter the question never states
     (`freight_to_france`), and revenue as SUM(UnitPrice) (`total_sales_revenue`).
   - [ ] `dropped_filter` costs more than it saves on Chinook dev (stopped-wrong 1,
     cost-right 2; Northwind 1 / 2). It still prevents wrong answers, so fix its
     misfires (unit conversions, values the plan expresses via another column)
     rather than remove it.
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

### Spider dev (unseen schemas) — 2026-10-04

400 seeded questions (seed 0) over 20 auto-catalogued databases, qwen2.5:7b @
845dbda0ea48, K=3 samples at T=0.7, Ollama with 4 parallel slots.
See [baselines/spider-dev400-sc3-2026-10-04.json](baselines/spider-dev400-sc3-2026-10-04.json);
`python scripts/consistency.py <that file>` reproduces the table.

| Policy | Answer-rate | Wrong | Wrong 95% hi | Precision |
|--------|-------------|-------|--------------|-----------|
| baseline (T=0 plan) | 26.8% | 16.0% | 19.9% | 62.6% |
| answer only if 3/3 samples agree | 22.5% | 7.2% | 10.2% | 75.6% |

- Chinook's ~1% wrong does not transfer. Hand review of 22 simple-question
  wrongs: ~11 real (dropped grouping, wrong key, COUNT DISTINCT for a list),
  ~6 measurement artifacts (tie order scored strictly, 1000-row cap),
  ~4 IR limits (no DISTINCT list), ~2 gold noise. Real wrong ≈ 11–13%.
- Nested / set-operation questions (95 of 400): 0 answerable by the IR, yet
  the model answers ~30% of them wrongly: 29 of 64 wrongs.
- Guards on unseen schemas: `dropped_concept` stopped 80 wrong / cost 9 right;
  `dropped_filter` 16 / 12.
- Coverage loss: 95 answerable questions end in `validation_failed`.

### LQP vs validated SQL on Spider dev — 2026-10-06

Same 400 questions, model, K=3 samples and scoring (LQP main verdicts re-scored:
[spider-dev400-sc3-2026-10-04.rescored.json](baselines/spider-dev400-sc3-2026-10-04.rescored.json);
SQL: [spider-dev400-sql-sc3-2026-10-05.json](baselines/spider-dev400-sql-sc3-2026-10-05.json)).
Right / wrong are shares of all 400 questions.

| Planner | Policy | Right | Wrong | Wrong 95% hi | Precision |
|---------|--------|-------|-------|--------------|-----------|
| LQP | baseline | 27.8% | 15.0% | 18.8% | 64.9% |
| LQP | 3/3 agree | 23.2% | 6.5% | 9.4% | 78.2% |
| SQL | baseline | 63.0% | 21.0% | 25.3% | 75.0% |
| SQL | 2/3 agree | 60.2% | 15.0% | 18.8% | 80.1% |
| SQL | 3/3 agree | 53.5% | 9.8% | 13.1% | 84.6% |

- Validated SQL gives 2.2–2.3× the right answers at every operating point,
  with higher precision. It answers nested/set questions the LQP cannot
  (22% / 16% right) but is wrong on 41% / 50% of them at baseline.
- The SQL path has none of the LQP semantic guards yet; on LQP,
  `dropped_concept` alone stopped 80 wrong answers.
- 20 SQL answers fail at execution (type mismatches on Spider's text-typed
  columns, ungrouped columns): no dry-run repair yet.
- Hand review of 16 SQL wrongs: ~4 gold noise, the rest real model errors
  (wrong column, missing/extra join, FK id instead of name, COUNT for SUM).
- Neither path is near the 2% target with qwen2.5:7b.

### Validated SQL with guards — 2026-10-06

Same 400 Spider dev questions. SQL path with fan-out check and the shared
semantic guards; `dropped_filter` language fix (number words, possessives,
short catalog words, adjective forms) measured by re-running the 35 cases it
refused ([merged result](baselines/spider-dev400-sqlguard-sc3-2026-10-06.merged.json)).

| Planner | Policy | Right | Wrong | Wrong 95% hi | Precision |
|---------|--------|-------|-------|--------------|-----------|
| LQP | 3/3 agree | 23.2% | 6.5% | 9.4% | 78.2% |
| SQL, no guards | 3/3 agree | 53.5% | 9.8% | 13.1% | 84.6% |
| SQL + guards | baseline | 57.8% | 13.8% | 17.5% | 80.8% |
| SQL + guards | 3/3 agree | 49.8% | 7.0% | 9.9% | 87.7% |

- At the LQP's wrong-rate, validated SQL answers 2.1× as many questions right.
- Guards on SQL: `dropped_concept` stopped 25 wrong / cost 14 right;
  `dropped_filter` (after the fix) 4 / 7, was 7 / 27.
- Still open: 18 execution errors (no dry-run repair), 27 model refusals,
  and the 2% target. Next evidence: Spider test (held out) and a stronger model.

### Model × planner on Spider dev — 2026-10-06

Same 400 questions, guards on, K=3. gpt-4.1-mini-2025-04-14 vs qwen2.5:7b.
Results: [SQL](baselines/spider-dev400-sql-gpt41mini-sc3-2026-10-06.json),
[LQP](baselines/spider-dev400-lqp-gpt41mini-sc3-2026-10-06.json).

| Model | Planner | Right (baseline) | Wrong (baseline) | Right (3/3) | Wrong (3/3) | Precision (baseline) |
|-------|---------|------------------|------------------|-------------|-------------|----------------------|
| qwen2.5:7b | LQP | 27.8% | 15.0% | 23.2% | 6.5% | 64.9% |
| qwen2.5:7b | SQL | 57.8% | 13.8% | 49.8% | 7.0% | 80.8% |
| gpt-4.1-mini | LQP | 52.2% | 11.8% | 43.5% | 6.2% | 81.6% |
| gpt-4.1-mini | SQL | 68.8% | 10.8% | 64.2% | 8.2% | 86.5% |

- The model explains most of the LQP gap (23% → 44% at 3/3); SQL still leads
  by 16–20 points of right answers at a similar wrong rate.
- With the stronger model, sample agreement helps little (errors are consistent),
  and the lexical guards on SQL cost more than they save: `dropped_concept`
  stopped 11 wrong / cost 32 right; `dropped_filter` 1 / 11.
- Hand review of 18 of the 43 SQL "wrong" answers: ~5 clear model errors,
  ~8 defensible readings of an ambiguous question (LEFT JOIN keeps zero-count
  rows, LIKE vs = for "republic", DISTINCT), ~5 gold or engine artifacts
  (SQLite text/number affinity, case-insensitive LIKE, a cross-join gold query).
  Exact-match scoring on Spider is now noisier than the errors it measures.

### Adjudicated wrong-rate — 2026-10-06

Two raters (developer + gpt-5, blind, conservative merge) on every disputed
case of the gpt-4.1-mini SQL run; see [adjudication/README.md](adjudication/README.md).
**Real wrong-rate 2.75% (11/400, 95% CI 1.5–4.9%)**, plus 4.5% ambiguous
questions answered under a defensible reading. 8 of 18 real errors are value
grounding (the model guesses stored spellings/case) — the next fix.

### Chinook dev on the SQL path: scoring fixes — 2026-10-07

gpt-4.1-mini, validated SQL, 132 dev cases. The suite was written for the
LogicalPlan path, so 11 of 14 "wrong" answers were correct. Fixed in the scorer
and dev cases only (holdout untouched), then re-scored the same run without new
model calls:

- Text time buckets ('2022-03') match the DATE_TRUNC start they name.
- 4 should-decline cases the IR cannot express get a `sql_reference`; the SQL
  planner is scored on the answer (avg per customer / employee / album,
  above-average customers).
- `subset_columns_ok` on 5 cases: "which X has the most Y" may omit Y; a
  customer list may omit SupportRepId.

**Wrong 14/132 (10.6%) → 3/132 (2.3%)**, no correct answer turned wrong. The
3 left are question readings: "albums do we sell" (sold vs offered),
"playlists customers listen to" (no listening data), and non-rep employees
counted in "customers per support rep".

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

- Running model-written SQL as written, or regex “SELECT-only” safety. Model SQL is
  parsed, validated and regenerated ([ADR 003](adr/003-validated-sql-planner.md))
- Growing LogicalPlan into a general SQL AST ([ADR 002](adr/002-ir-boundary.md))
- Sending result cells to an LLM ([ADR 001](adr/001-result-policy.md))
