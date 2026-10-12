# Plan

**Updated:** 2026-10-11 · Results live in [STATUS.md](STATUS.md); this file is the order of work.

## The goal (unchanged — do not move it)

On **one real domain**, with equal curation effort, Secure Query is wrong
measurably less often than Databricks Genie, at an answer-rate users accept.

| Metric | Bar |
|--------|-----|
| Wrong-rate, 95% upper bound | ≤ 2%, and below Genie's |
| Answer-rate on answerable questions | ≥ 70% |
| Holdout | ≥ 200 real questions, 5 repeats |

What we can claim today: **the model's SQL never runs.** The kernel regenerates
it from a validated tree and enforces policy there. An independently graded
wrong-rate and a flip rate are things we have not published yet (mnemiq already
publishes both); they are claims to earn, not to make.

## Rules (read before starting any work)

1. **One step in progress at a time.** Work the steps below in order. A step is
   done only when its "done when" line is true and recorded in STATUS.md.
2. **Every change names the bar metric it moves** and is measured on the
   regression set before merge: Chinook holdout, Chinook dev, Northwind,
   Northwind dev, Spider dev 400. The regression set is a smoke test; a green
   run is not permission to skip a step.
3. **BIRD is a stress test, not a target.** Run it at milestones to find
   failure modes; never tune against it and never put it in a "done when".
4. **New ideas go to the parking lot**, not into the current step.
5. **Wrong-rate claims are adjudicated** (see [adjudication/](adjudication/README.md)),
   on a set we have not iterated on. Spider dev is contaminated; Spider test is
   used once per milestone.
6. **Execution errors are safe failures.** Do not feed them back to the model
   to make a query run; fix dialect bugs deterministically in the compiler.
7. **No threshold search.** An operating point is chosen once, before the run
   that measures it.
8. Dataset-agnostic only: domain knowledge goes in catalog data, never in code.
9. Every number in this file or a commit message is written down in STATUS.md.

## Critical path (owner: Sam)

- [ ] **Partner date — set before Step 3 starts.** First milestone: ________
  (e.g. outreach list sent to N candidate design partners).
- [ ] **Design partner and real domain (Phase B).** Done when: named owner in
  OWNERSHIP.md, an approved catalog for one slice of their warehouse, ≥ 200 real
  questions frozen before any tuning, and Genie available on the same data.

## Engineering steps (in order)

### Step 1 — Smoke-test what we shipped ✅ 2026-10-11
- [x] Regression set on the SQL path, gate vs old guards. Found and fixed ties
  at the LIMIT boundary (kernel tie-break). Adjudicated: holdout 0 real wrong,
  real wrongs equal elsewhere, +9 right. Spider test (and its 'yes' retry case)
  not covered — that is Step 2.

### Step 2 — Real wrong-rate, once, on Spider test
- [ ] Run Spider test 400 on the current system, 3 repeats.
- [ ] Adjudicate every wrong answer → real wrong-rate with 95% CI.
- [ ] Flip rate across the 3 repeats (our own stability number; not a Genie
  comparison — that needs the same harness, Step 3).
- [ ] Check the 'yes' retry case that `dropped_filter` used to stop.
- Done when: STATUS.md has the adjudicated wrong-rate, CI, flip rate and the
  'yes' case outcome. This result is the baseline Step 5 must beat.

### Step 3 — Genie harness test on Databricks (not the comparison)
- [ ] Get a Databricks workspace with a SQL warehouse and a Genie space.
- [ ] **Gate:** one question answered by Genie through the Conversation API,
  by hand. Do not start the client before this works.
- [ ] Client: ask Genie and Secure Query the same questions on the same
  Databricks data; grade both with our grader; record right / refused / wrong
  and flip rate.
- Done when: one end-to-end run on a small dataset we fix in advance (fixed
  curation budget for both). **This is a harness test. It becomes the real
  comparison only on a partner's data (Step 7).**

### Step 4 — Dialect fixes in the compiler (only if Step 2 says so)
- [ ] If the Step 2 adjudication shows execution errors cost real answers,
  add deterministic rewrites in the kernel (e.g. SQLite-style
  `strftime(fmt, col)` to the target dialect). No model repair of execution
  errors.
- Done when: execution errors lower on the regression set, wrong-rate not
  higher.

### Step 5 — Precision experiment: second reading from another provider
- [ ] Experiment, not a promise: the gate's second reading from Claude, and
  separately an LLM judge on the first query. (arXiv 2607.06799 measured judge
  ensembles on one query at 0.82 AUROC; it did not measure a second generator,
  and its usable point was 27% answered at 24% risk.)
- [ ] Report a risk–coverage curve on Spider test.
- Done when: the curve is in STATUS.md. **Stop if it does not beat the Step 2
  result.** No threshold search.

### Step 6 — Real domain (needs the critical path)
- [ ] Certified definitions: metrics and glossary entries with a named owner
  in catalog data.
- [ ] Freeze the partner's ≥ 200 questions as the holdout; tune only on dev.

### Step 7 — Head-to-head (Phase C, the goal)
- [ ] The Step 3 harness on the partner's data: same questions, same curation
  effort, Secure Query vs Genie, graded independently.

## Parking lot (not now — needs evidence and a finished step)

- Comparison through mnemiq's grader (beacon): same grader, different
  conditions; context only.
- Bigger model of the same family (tested: gpt-4.1 ≈ gpt-4.1-mini, ~5× cost).
- Pairwise selection among samples (tested: ≤ 2 points of headroom).
- More samples in the gate (3 or 5 agree ≈ 2 agree on precision).
- Model repair of execution errors (turns safe failures into unverified answers).
- LLM-generated schema descriptions, retrieval depth, few-shot examples,
  fine-tuned verifier.
- Re-scoring the LQP-era suites for the SQL path (count columns, ratios).
- Any BIRD-specific tuning; any LQP-path change.

## Decision log

| Date | Decision | Evidence |
|------|----------|----------|
| 2026-10-11 | Keyword guards removed from the SQL path; two-reading agreement gate | STATUS.md, 2026-10-11 section |
| 2026-10-11 | Keep gpt-4.1-mini | gpt-4.1 within noise at ~5× cost |
| 2026-10-11 | This plan; BIRD demoted to stress test | Distraction review: work had drifted from the bar |
| 2026-10-11 | Kernel breaks ties under ORDER BY … LIMIT | Gate false-blocked 11 Chinook answers; fixed to 2 |
| 2026-10-11 | Plan revised after advisor review | Overclaim removed; paper citation corrected; Genie harness moved ahead of precision work; partner date gates Step 3 |
