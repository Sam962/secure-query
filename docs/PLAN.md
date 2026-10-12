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

What we claim that others don't: **the model's SQL never runs** (the kernel
regenerates it from a validated tree and enforces policy there), and **our
wrong-rate is published, independently graded and stable** — same question,
same answer.

## Rules (read before starting any work)

1. **One step in progress at a time.** Work the steps below in order. A step is
   done only when its "done when" line is true and recorded in STATUS.md.
2. **Every change names the bar metric it moves** and is measured on the
   regression set before merge: Chinook holdout (must stay 0 wrong), Chinook
   dev, Northwind, Northwind dev, Spider dev 400.
3. **BIRD is a stress test, not a target.** Run it at milestones to find
   failure modes; never tune against it. Nobody is near 2% wrong on BIRD.
4. **New ideas go to the parking lot**, not into the current step. An idea
   leaves the parking lot only when the current step is done and evidence says
   it moves the bar.
5. **Wrong-rate claims are adjudicated** (see [adjudication/](adjudication/README.md)),
   not raw grader output.
6. Dataset-agnostic only: domain knowledge goes in catalog data, never in code.

## Critical path (owner: Sam, starts now, runs in parallel)

- [ ] **Design partner and real domain (Phase B).** Longest lead time; nothing
  below substitutes for it. Done when: named owner in OWNERSHIP.md, an approved
  catalog for one slice of their warehouse, and ≥ 200 real questions frozen
  before any tuning, with Genie access on the same data.

## Engineering steps (in order)

### Step 1 — Validate what we just shipped
- [x] Run the regression set on the SQL path, self-consistency gate vs the old
  guards. Found and fixed: ties at the LIMIT boundary (kernel tie-break).
- Done when: holdout still 0 wrong and no suite's wrong-rate is worse. If the
  holdout regresses, fix or revert the gate before Step 2.
- **Done 2026-10-11 (adjudicated):** holdout 0 real wrong (3 scoring artifacts,
  same under both versions); real wrongs equal elsewhere, +9 right. Open
  consequence for Step 2: the suites were written for the LQP path and
  mis-score SQL-path answers (missing count columns, ratios it can compute).

### Step 2 — Make the numbers trustworthy
- [ ] Adjudicate the wrong answers of the gate run on Spider dev 400 and Spider
  test 400 → real wrong-rate with CI.
- [ ] Add a **stability** column to the eval report: answer flip rate across
  repeats (Genie: 9–17% on public benchmarks; this is a trust claim we can
  measure cheaply).
- Done when: STATUS.md has the adjudicated wrong-rate and flip rate for the
  current system.

### Step 3 — Stop avoidable failures
- [ ] Feed execution errors (dialect mismatches such as SQLite `strftime` on
  DuckDB) back into the existing repair round. Evidence: mnemiq measured ~7
  points from a dialect fix; we lose 25–50 BIRD questions per run to errors.
- Done when: execution errors roughly halved on Spider and BIRD with no
  wrong-rate increase on the regression set.

### Step 4 — Precision: an independent second reading
- [ ] Make the gate's second reading come from a **different provider**
  (e.g. Claude). Evidence: same-model self-consistency plateaus at 0.61–0.68
  AUROC with no low-risk subset; a cross-provider pair reaches 0.82
  (arXiv 2607.06799). Needs a second API key.
- [ ] Measure risk vs coverage; pick the operating point against the bar.
- Done when: adjudicated wrong-rate on Spider test is lower at ≥ 70% answer-rate.

### Step 5 — First external comparison with Genie (proxy for Phase C)
- [ ] Check that beacon (independent grader, github.com/agenticfabriq/beacon)
  exposes Genie's per-question BIRD / Spider results. If yes, grade our runs
  there for a like-for-like comparison including flip rate.
- Done when: one table, same grader, Secure Query vs Genie vs mnemiq — right,
  refused, wrong and stability.

### Step 6 — Real domain (Phase B, needs the critical path)
- [ ] Certified definitions: metrics and glossary entries with a named owner
  in catalog data (evidence: mnemiq 69–72% with vs 52–59% without).
- [ ] Freeze the partner's ≥ 200 questions as the holdout; tune only on dev.

### Step 7 — Head-to-head (Phase C)
- [ ] Same questions, same curation effort, Secure Query vs Genie on the
  partner domain, graded independently. This is the goal.

## Parking lot (not now — needs evidence and a finished step)

- Bigger model of the same family (tested: gpt-4.1 ≈ gpt-4.1-mini, ~5× cost).
- Pairwise selection among samples (tested: ≤ 2 points of headroom).
- More samples in the gate (3 or 5 agree ≈ 2 agree on precision).
- LLM-generated schema descriptions (mnemiq: +1.4 BIRD, ~0 on Spider).
- Retrieval depth, few-shot examples, fine-tuned verifier.
- Any BIRD-specific tuning; any LQP-path change.

## Decision log

| Date | Decision | Evidence |
|------|----------|----------|
| 2026-10-11 | Keyword guards removed from the SQL path; two-reading agreement gate | STATUS.md, 2026-10-11 section |
| 2026-10-11 | Keep gpt-4.1-mini | gpt-4.1 within noise at ~5× cost |
| 2026-10-11 | This plan; BIRD demoted to stress test | Distraction review: work had drifted from the bar |
| 2026-10-11 | Kernel breaks ties under ORDER BY … LIMIT | Gate false-blocked 11 Chinook answers; fixed to 2 |
