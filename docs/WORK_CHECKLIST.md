# Secure Query — Work Checklist

**Repo:** `/Users/samjordan/Desktop/secure-query`  
**Use with:** [ROADMAP.md](ROADMAP.md) (details) · [PLAN.md](PLAN.md) (context)

Tick boxes as you complete work. **Rule:** never tune prompts/guards using the holdout set.

---

## Before you start (one-time setup)

- [ ] Open repo in Cursor: `/Users/samjordan/Desktop/secure-query`
- [ ] `python -m venv .venv && source .venv/bin/activate`
- [ ] `pip install -e ".[dev,planner]"`
- [ ] `python -m secure_query.examples.load_sample_db`
- [x] `pytest -q` passes *(302, 2026-08-11)*
- [ ] Ollama or API key configured (optional for planner work)
- [ ] Read [DEMO.md](DEMO.md) once

---

## Milestone A — Trustworthy demo

**Gate:** holdout wrong-rate = **0**, dev suite ≥ 100 questions, synonyms + metrics v0, CI green.

### Epic 1 — Eval discipline (P0) · ~4 days

- [x] **1.1** Create `secure_query/evals/dev.json` + `holdout.json` from `chinook_live.json` (70/30) — via `suite_loader.py` + `dev_expansion.py`
- [ ] **1.2** Tag each case: `intent`, `tables`, `pii`, `id`
- [x] **1.3** Add `--split dev|holdout|all` to `run_chinook.py`
- [x] **1.4** Print separate accuracy / wrong-rate / refusal-rate per split
- [x] **1.5** Expand dev suite to **100+** questions (101 dev cases)
- [ ] **1.6** Wire `--repeat N` stability report (plan agreement %)
- [ ] **1.7** Write `docs/EVAL.md` (holdout rule, how to add cases)
- [x] **1.8** Baseline run saved: `docs/baselines/holdout-2026-08-11.txt`

**Done when**
- [ ] Holdout never used in prompt/guard commits (document in EVAL.md)
- [ ] `pytest` + `run_chinook --split holdout --accuracy` documented in README

---

### Epic 2 — Catalog synonyms (P0) · ~3 days

- [x] **2.1** Add `Synonym` model to `catalog.py`
- [x] **2.2** Add `synonyms: list[Synonym]` to `Catalog`
- [x] **2.3** Include synonyms in `planner_summary()`
- [x] **2.4** Update `guard.dropped_concepts` to honor synonyms before refusing
- [x] **2.5** Add Chinook synonyms in `sample_catalog.py` (~20 terms):
  - [x] `revenue`, `sales`, `spend` → `Invoice.Total`
  - [x] `genre`, `music genre` → `Genre.Name`
  - [x] `country`, `billing country` → `Invoice.BillingCountry` or `Customer.Country` (document which)
  - [ ] `customer`, `client` → `Customer` table
- [x] **2.6** Unit tests for synonym matching in guards
- [x] **2.7** Eval case: `top_genre` — correct answer or refusal (never wrong substitute)
- [x] **2.8** Re-run holdout; record result in baseline file

**Done when**
- [x] `top_genre` stable failure fixed or refuses cleanly
- [x] Holdout wrong-rate still **0**

---

### Epic 3 — Metric registry v0 (P0) · ~6 days

- [x] **3.1** Create `secure_query/metrics.py` with `MetricSpec`
- [x] **3.2** Attach metrics to `Catalog`
- [x] **3.3** Define expansion: metric id → `LogicalPlan` (server-side, not LLM)
- [x] **3.4** Seed metrics in `sample_catalog.py`:
  - [x] `total_revenue`
  - [x] `invoice_count`
  - [x] `avg_revenue_per_customer`
  - [x] `revenue_by_country`
  - [x] `revenue_by_genre`
- [x] **3.5** Planner prompt: list metrics; model may return `{"metric_id": "..."}` 
- [x] **3.6** `expand_metric()` before validate
- [x] **3.7** Validator tests for metric-expanded plans
- [ ] **3.8** Eval: ratio/per-unit questions that previously refused
- [ ] **3.9** Golden tests for each metric → expected SQL
- [ ] **3.8** Re-run holdout; record result

**Done when**
- [x] “Average revenue per customer” uses blessed definition (not `AVG(Total)`)
- [x] Holdout wrong-rate **0**

---

### Epic 4 — Label / lookup columns (P1) · ~4 days

- [x] **4.1** Add `display_column` (or FK label map) on catalog tables
- [x] **4.2** Chinook: `Genre.Name`, `MediaType.Name`, `Artist.Name`, etc.
- [x] **4.3** Expose in `planner_summary()` (“group by Genre.Name for genre”)
- [ ] **4.4** Eval: “tracks per genre name”, “top genre by revenue”
- [ ] **4.5** Re-run holdout

**Done when**
- [x] Genre breakdown questions group by name, not raw id
- [x] Holdout wrong-rate **0**

---

### Epic 5 — Security checklist + CI (P0) · ~2 days

- [x] **5.1** CI workflow `.github/workflows/ci.yml`:
  - [x] `pytest -q`
  - [x] `run_chinook --split holdout --accuracy` (mock or pinned model)
  - [x] Grep gate: no f-strings in `compile.py`
  - [ ] Grep gate: planner tests assert no SQL in model output
- [ ] **5.2** Write `docs/SECURITY.md` (threat model + evidence)
- [ ] **5.3** Update README security checklist — check only what CI proves:
  - [ ] LLM never outputs SQL
  - [ ] validate before compile
  - [ ] AST-only compile
  - [ ] execute RO + timeout + limit
  - [ ] audit trail
- [ ] **5.4** Integration test: query timeout actually interrupts

**Done when**
- [ ] Green CI on main
- [ ] README checklist matches CI

---

### Milestone A sign-off

> **2026-08-11:** Holdout **12/12 OK**, 0 wrong, 0 abstained — Ollama `qwen2.5:7b` (verified `c822deb2`). Fixes: guards, `normalize_plan`, `employee_count` metric, genre/artist `useless_joins`.

- [x] Holdout: **0 wrong**, n ≥ 12
- [x] Dev suite: n ≥ 100 (101 cases)
- [x] Synonyms + metrics v0 shipped
- [ ] CI green
- [ ] Tag release: `v0.2.0-trustworthy-demo`

---

## Milestone B — First production domain

**Gate:** auth works, API live, summarizer policy enforced, domain eval before go-live.

### Epic 6 — Authorization & tenant isolation (P0) · ~6 days

- [x] **6.1** `Principal` model (`tenant_id`, `roles`, `row_predicates`)
- [x] **6.2** `Catalog.for_principal(principal) -> Catalog`
- [x] **6.3** Compile-time inject mandatory filters (non-droppable)
- [x] **6.4** Validator rejects out-of-scope tables/columns for principal
- [x] **6.5** Audit: `principal_id`, `tenant_id`
- [x] **6.6** Tests: cross-tenant read attempt → rejected
- [x] **6.7** Document in `SECURITY.md`

**Done when**
- [x] Two-tenant test proves no cross-read

---

### Epic 7 — Summarizer / result policy (P1) · ~4 days

- [ ] **7.1** ADR `docs/adr/001-result-policy.md` (pick default)
- [ ] **7.2** Default: template answer from `explain_plan()` + result table (no LLM)
- [ ] **7.3** Optional `summarize.py`: LLM on aggregates only, block PII columns
- [ ] **7.4** Audit flags: summarizer used, rows seen
- [ ] **7.5** README documents policy

**Done when**
- [ ] Policy written and default path needs no extra LLM

---

### Epic 8 — Clarify & confirm UX (P1) · ~5 days

- [ ] **8.1** Structured clarify reasons in API response schema
- [ ] **8.2** Ambiguity detector → clarify (multiple metrics/tables match)
- [ ] **8.3** `--confirm` mode: show explain-back before execute
- [ ] **8.4** `ask_sample.py --confirm` demo
- [ ] **8.5** Eval: ambiguous questions → clarify, not guess

**Done when**
- [ ] No silent default on ambiguous questions
- [ ] Confirm gate works in CLI demo

---

### Epic 9 — HTTP API (P1) · ~5 days

- [ ] **9.1** FastAPI: `POST /ask`, `POST /ask/confirm`, `GET /health`
- [ ] **9.2** Auth header → `Principal`
- [ ] **9.3** Config via env (catalog, db, model provider)
- [ ] **9.4** Docker Compose demo
- [ ] **9.5** OpenAPI + curl examples in README
- [ ] **9.6** Client cannot send SQL in request body (test)

**Done when**
- [ ] One curl: question → explain → results → audit line

---

### Milestone B sign-off

- [ ] Auth + tenant filters proven in tests
- [ ] API deployed locally via Docker
- [ ] Summarizer policy ADR merged
- [ ] Domain eval + holdout run before calling it “prod-ready”
- [ ] Tag release: `v0.3.0-prod-domain`

---

## Milestone C — Company scale

**Gate:** multi-domain, retrieval, catalog pipeline, second dialect, ownership doc.

### Epic 10 — Postgres dialect (P2) · ~5 days

- [ ] **10.1** Postgres golden tests (subset of Chinook plans)
- [ ] **10.2** `execute_postgres()` with timeout + row cap + audit
- [ ] **10.3** Dialect-specific edge cases documented
- [ ] **10.4** README: how to switch dialect

---

### Epic 11 — Table retrieval (P2) · ~6 days

- [ ] **11.1** Embed table/column descriptions
- [ ] **11.2** `retrieve_tables(question, catalog, k)` 
- [ ] **11.3** Validate against **full** authorized catalog (not slice)
- [ ] **11.4** Retrieval recall eval on dev suite

---

### Epic 12 — Domain routing (P2) · ~5 days

- [ ] **12.1** Domain registry (id, catalog path, eval path)
- [ ] **12.2** Router: question → domain
- [ ] **12.3** API `domain` param
- [ ] **12.4** Per-domain go-live checklist template

---

### Epic 13 — Catalog pipeline (P2) · ~6 days

- [ ] **13.1** `scripts/draft_catalog.py` from `information_schema`
- [ ] **13.2** YAML output for PR review
- [ ] **13.3** CI: catalog change → goldens + holdout
- [ ] **13.4** Schema drift alert job

---

### Epic 14 — IR boundary ADR (P3) · ~1 day

- [ ] **14.1** `docs/adr/002-ir-boundary.md` — what IR never does
- [ ] **14.2** Analyst handoff when question exceeds IR

---

### Epic 15 — Red team & fuzz (P2) · ~5 days

- [ ] **15.1** 50+ adversarial planner prompts
- [ ] **15.2** Hypothesis fuzz: 10k random valid plans → compile → parse
- [ ] **15.3** Property: tenant filter always in compiled SQL
- [ ] **15.4** Results in `SECURITY.md`

---

### Epic 16 — Org & ownership (P0 for scale) · non-code

- [ ] **16.1** `docs/OWNERSHIP.md` — RACI per domain
- [ ] **16.2** Catalog owner named per domain
- [ ] **16.3** Metric approval process defined
- [ ] **16.4** Schema change SLA defined
- [ ] **16.5** Release policy signed: wrong-rate = 0 gate
- [ ] **16.6** Build vs buy note (optional)

---

### Milestone C sign-off

- [ ] ≥1 real domain live with own catalog + holdout
- [ ] Retrieval + routing if catalog > 50 tables
- [ ] Catalog pipeline in CI
- [ ] Postgres path tested
- [ ] OWNERSHIP.md complete

---

## Weekly rhythm (suggested)

| Day | Focus |
|-----|--------|
| Mon | Pick epic tasks; run holdout baseline |
| Tue–Thu | Implement + unit tests |
| Fri | Expand eval cases; run holdout; update baseline file |
| Never | Tune prompts/guards on holdout |

---

## Commands cheat sheet

```bash
cd /Users/samjordan/Desktop/secure-query
source .venv/bin/activate

pytest -q
python -m secure_query.examples.demo_catalog
python -m secure_query.examples.ask_sample "revenue by country"

# After Epic 1:
python -m secure_query.evals.run_chinook --split dev --accuracy --provider ollama
python -m secure_query.evals.run_chinook --split holdout --accuracy --provider ollama

# Stability:
python -m secure_query.evals.run_chinook --split holdout --accuracy --repeat 3 --provider ollama
```

---

## Start here (first 5 checkboxes)

1. [ ] Setup block above (venv, tests green)
2. [ ] Epic 1.1 — split dev / holdout
3. [ ] Epic 1.3 — `--split` flag in runner
4. [ ] Epic 2.1 — `Synonym` on catalog
5. [ ] Epic 5.1 — CI workflow skeleton

When those five are done, you have trustworthy measurement + first semantic fix + automation.
