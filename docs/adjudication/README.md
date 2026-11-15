# Adjudication: Spider dev, validated SQL, gpt-4.1-mini (2026-10-06)

Exact-match scoring marked 43 of 400 answers wrong (10.8%). On inspection many
were not mistakes, so every disputed case was labelled by two raters.

## Method

- **Cases:** the 43 answered-wrong cases, and the 12 answers a guard blocked
  that also mismatched gold (55 total). The 41 blocked answers that matched gold
  are not adjudicated (counted as right answers the guards cost).
- **Labels (fixed before rating):**
  - `real_error`: a careful analyst would call the answer wrong; a user would be misled.
  - `ambiguous`: the question genuinely allows the system's reading, and the answer is right under it.
  - `benchmark_fault`: the gold query is wrong, or the difference is an engine/format artifact.
- **Raters:** the developer (labels with reasons, written before seeing the
  model's), and gpt-5-2025-08-07 with the rubric in
  `src/secure_query/evals/adjudicate.py`, blind to the human labels.
- **Merge:** conservative — `real_error` if either rater says so.
- Agreement: 38/55 on the exact label, 46/55 on error vs not-error.

Files: `*-packets.json` (question, schema, gold and system SQL and rows),
`*-labels-human.json`, `*-labels-gpt5.json`, `*-labels-merged.json`.

## Result (answers the product returned, n = 400)

| Count | Share | 95% CI | What it is |
|-------|-------|--------|------------|
| 11 | 2.75% | 1.5–4.9% | real errors (conservative merge) |
| 18 | 4.5% | — | ambiguous question, defensible reading |
| 14 | 3.5% | — | benchmark faults (system right) |

Real errors among the 12 blocked mismatches: 7. Turning the SQL guards off would
add those 7 real errors and recover ~41 right answers.

## What the real errors are

8 of the 18 real errors (answered + blocked) are **value grounding**: the model
cannot see stored values, so it guesses spelling or case ('Europe' vs 'europe',
'Jetblue' vs 'JetBlue', 'math' vs 'Math', 'North Carolina' vs 'NorthCarolina',
a title missing its '!', a make/model split). The rest are logic: "A and B"
read as either, NOT IN at the wrong grain, grouping by a non-unique name.

## Caveats

- One human rater, who also built the system; the model rater and the
  conservative merge are the check on that. A second human rater is better.
- Answers that matched gold were not re-examined; some may share a gold fault.
- 400 questions from one benchmark; the held-out Spider test split is the next check.

---

# Spider test (held out), validated SQL, gpt-4.1-mini (2026-10-07)

Same method, rubric and merge as dev above. 400 seeded test questions (seed 0,
K=3) over 40 databases never used for development; SQL planner with value
grounding. Exact match: 296 right, 34 wrong (8.5%). Disputed: the 34
answered-wrong cases and 6 guard-blocked mismatches (40). Agreement 26/40
exact, 30/40 error vs not-error.

| Count | Share | 95% CI | What it is |
|-------|-------|--------|------------|
| 15 | 3.75% | 2.3–6.1% | real errors (conservative merge) |
| 11 | 2.75% | — | ambiguous question, defensible reading |
| 8 | 2.0% | — | benchmark faults (system right) |

Four of the 15 are gpt-5 calls the data contradicts (a tie for "most orders",
two SQLite text-vs-number comparisons in the gold, the 10 000-row cap); without
them 11/400 (2.75%). The conservative figure is the headline.

Real errors by kind: value matching 3 ('United States' for stored 'USA',
case-sensitive LIKE '%unsatisfied%', is_buyer = 'Y' for stored 1) plus 1
blocked ('Mckeown' for 'Mckeown, Kathleen'); logic: HAVING vs WHERE, LIMIT 1
dropping a tie, counting non-owners as owners, a nonsensical RIGHT JOIN,
DISTINCT merging rows. None of the value cases is a spelling variant, so
grounding (which fixed 7 on dev) did not apply.

Files: `spider-test-gpt41mini-*.json`; run:
`../baselines/spider-test400-sql-gpt41mini-sc3-2026-10-07.json`.

---

# Spider test, SQL path with self-consistency gate (2026-10-11)

Same 400 questions (seed 0), gpt-4.1-mini, 3 repeats; a question's verdict is
its worst repeat and the packet holds that repeat's rows. 315 right, 33 wrong.

- **Reused:** 14 of the 33 return the same rows as an answer labelled on
  2026-10-07 (checked on full rows, not SQL text); that merged label is kept.
- **Rated:** the other 19 by two raters: gpt-5-2025-08-07, blind, and a second
  rater written before gpt-5's labels were seen. **The second rater is Claude
  (claude-opus-5-5), not a human**; a human pass on these 19 is still open.
- **Rubric change:** the tie rule is now written into the rubric: a single row
  is ambiguous only when the question asks for one and the row is truly tied
  for the maximum; dropping one side of a tie at a "top N" cutoff is a real error.
- **Merge:** real_error if either rater says so; no case dropped afterwards.
  Raters disagreed on 6 of 19; 2 of those became real errors through gpt-5.

| Count | Share | 95% CI | What it is |
|-------|-------|--------|------------|
| 11 | 2.75% | 1.5–4.9% | real errors (conservative merge) |
| 15 | 3.75% | — | ambiguous question, defensible reading |
| 7 | 1.75% | — | benchmark faults (system right) |

Files: `spider-test-gpt41mini-gate-2026-10-11-*.json`; run:
`../baselines/spider-test400-gpt41mini-gate-r3-2026-10-11.json`.

---

# Head-to-head rounds (Secure Query vs Genie)

```
python -m secure_query.evals.scorecard --suite SUITE.json --backend databricks --repeat 3 --out scorecard.json
python scripts/adjudicate.py packets scorecard.json -o packets.json
```

`packets.json` goes to the raters: wrong answers of both systems, shuffled and numbered,
with no system name. `packets.key.json` maps each number back to its system and case; keep
it away from the raters until the labels are in.

---

# Spider test (held out), validated SQL with the gate, gpt-6-luna (2026-11-12)

Same 400 seeded questions, rubric and conservative merge as above. `gpt-6-luna` with
reasoning off, self-consistency gate on, 3 repeats, after the correlated-subquery
validator fix and the refusal-rule prompt change. Exact match: 307 right, 40 wrong, 53
refused.

- **Cases:** the 40 answered-wrong cases. 13 return the same rows as a case labelled in an
  earlier round and reuse that merged label (`*-labels-reused.json`). The other 27 were
  rated new.
- **Raters for the 27:** gpt-5-2025-08-07 with the rubric, and a second rater that is a
  model, not a human (Claude, `claude-fable-5-2`), which wrote its labels before seeing
  gpt-5's. Agreement: 20/27 on the exact label, 25/27 on error vs not-error.
- **No human rated this round.**

| Count | Share | 95% CI | What it is |
|-------|-------|--------|------------|
| 14 | 3.5% | 2.1–5.8% | real errors (conservative merge) |
| 15 | 3.75% | — | ambiguous question, defensible reading |
| 11 | 2.75% | — | benchmark faults (system right) |

For comparison, `gpt-4.1-mini` on 2026-10-11 had 11 real errors (2.75%, CI 1.5–4.9%) with
78.8% right; the intervals overlap. The real errors are logic errors: a filter applied
before an aggregate instead of after, "A and B" read as one row holding both, a missing
SUM per group, a wrong column counted, an inner join that drops a group.

Files: `spider-test-gpt6luna-gate-2026-11-12-packets.json` and `-labels-*.json`.

---

# Spider test, gpt-6-luna, run of 2026-11-13: two new wrong answers and an audit of right answers (2026-11-14)

Run: `../baselines/spider-test400-gpt6luna-gate-r3-2026-11-13.json` (310 right, 38 wrong,
52 refused). **No human rated anything in this section.**

**Two wrong answers without a label** (`*-new2-*`): gpt-5-2025-08-07 with the rubric, and
Claude (`claude-fable-5-2`), which wrote its labels before seeing gpt-5's. They agree:
`test_1041_aan_1` real error, `test_0453_cre_Students_Information_Systems` ambiguous. With
the 36 labels carried over by question id, the run has 15 real errors of 400 (3.75%, 95% CI
2.3–6.1%).

**Audit of answers scored right** (`*-right60-*`). A match on one database does not prove
the query (SpotIt, arXiv 2510.26840). 60 of the 310 right answers, seed 0:

```
python scripts/adjudicate.py packets REPORT.json --split test --right 60 -o right60-packets.json
python scripts/adjudicate.py rate right60-packets.json -o labels.json --model gpt-5-2025-08-07
```

Labels: `sound`, or `hidden_error` when the rater can name plausible data on which a careful
analyst would call the answer wrong (`RIGHT_RUBRIC` in the script).

| Rater | hidden_error of 60 |
|-------|--------------------|
| gpt-5, first rubric (`*-labels-gpt5-first-rubric.json`) | 40, of which 25 only for the 10,000-row cap |
| gpt-5, current rubric (`*-labels-gpt5.json`) | 11 |
| Claude, on the 40 first-pass flags, **not blind** (`*-labels-second-rater.json`) | 1 |

The rubric was changed once, after the first pass: the row cap, a single row where rows
could tie, and left-out zero-count groups are not errors. The one case both the second rater
and gpt-5 flag is `test_1168_pilot_1`, for different reasons. Of gpt-5's other 10, most depend
on a NULL or on a key the schema forbids being broken, and most are the reference's own
query. This spread is too wide to quote a rate. A human pass on the 60 packets is open.

## Head-to-head packets (Genie), 2026-11-14 and 2026-11-15

`genie-h2h-spider-{aan1,bakery1,advertising}-*`: one packet per answer scored wrong, both
systems shuffled together with no system name; `*-packets.key.json` maps a packet to its
system and must not go to a rater. Labels: `labels-gpt5` (gpt-5), `labels-second-rater`
(the assistant, a model, which could tell the systems apart by SQL style), `labels-merged`
(real_error if either says so). No person has rated these. Result in STATUS, 2026-11-15.
