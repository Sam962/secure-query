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
