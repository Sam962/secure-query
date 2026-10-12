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
