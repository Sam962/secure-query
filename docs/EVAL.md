# Eval protocol

## Splits

| Split | Purpose | Rule |
|-------|---------|------|
| **dev** | Tune prompts, guards, synonyms | OK to iterate |
| **holdout** | Release gate | **Never tune on holdout** |
| **all** | Full picture only | Do not use for tuning |

Holdout ids are frozen in `src/secure_query/evals/dev_expansion.py` (`HOLDOUT_CASE_IDS`).

## Commands

```bash
export PYTHONPATH=src
pytest tests/test_suite_split.py -q
python -m secure_query.evals.run --split dev --provider mock            # wiring only
python -m secure_query.evals.run --split holdout --provider openai      # SQL path (default)
python -m secure_query.evals.run --split holdout --provider openai --planner lqp
```

`--planner sql` is the default and matches the product. With a real model it also runs
the self-consistency gate (two readings), exactly as `/ask` does.

**Pin the model build, not just the name.** Record the model id in every baseline. For
local Ollama models the runner prints `digest=`; pass `--expect-digest <prefix>` (or set
`SECURE_QUERY_MODEL_DIGEST`) and it refuses to run against another build (exit 2). A
re-pulled `qwen2.5:7b` once moved the holdout from 0 to 2 wrong with an identical prompt.

## How scores work

- **accuracy** includes expected refusals: if the case is `expect=abstain` and the kernel declines, that is `[OK]`.
- The **abstained** headline is *unexpected* refusals on questions that should have been answered.
- **wrong** (plus unsafe PII leak) is the release gate. `evals.run` exits 1 on any wrong or unsafe answer, not on raw accuracy.
- **Ties at a LIMIT:** when a reference query's LIMIT cuts through a tie ("top 3" with France and Brazil tied for third), any tie-breaking is correct. The harness accepts a result whose rows all exist in the un-LIMITed reference and whose numeric values match the reference top-N exactly. Returning a lower-ranked row is still wrong.

### Report fields

| Field | Meaning |
|-------|---------|
| `wrong answers … 95% CI` | Wilson interval on wrong + unsafe. Quote the upper bound, not the point estimate: 0/12 still allows ~24% |
| `answer-rate` | Correct answers over **answerable** cases only (`expect=answer`) |
| `over-refusal` | Answerable cases the system declined — the coverage cost of the guards |
| `refusals by control` | Per `clarify_code`: `correct` = declined a question that should be declined. On answerable questions the harness runs the plan the guard blocked: `stopped-wrong` = it would have been a wrong answer, `cost-right` = it would have been right, `false` = no plan to score (model refusal). A control is a removal candidate only when `cost-right` exceeds `correct` + `stopped-wrong`. Counting every answerable refusal as a cost is misleading: on 2026-10-04 `dropped_concept` showed 0 correct / 12 false on Chinook, yet all 12 blocked plans were wrong |
| `by tag` | Verdicts per case tag (topn, join, filter, …) |

`--json PATH` writes the summary plus per-case verdict, refusal code, and SQL. Use it for comparisons (e.g. against Genie on the same questions) rather than parsing stdout.

The CI mock gate checks wiring, not quality: the mock planner refuses most questions, so it shows 0% wrong at ~3% answer-rate. Only a live-model run measures the product.

Four Chinook holdout cases are frozen as abstain: `list_all_emails`, `supplier_spend`, `revenue_growth_rate`, `avg_revenue_per_customer`. Do not flip those expects as a tuning trick even if a ratio metric exists.

## Second schema (Northwind)

Checks that the planner, guards and prompt work on a schema they were not
written against. Everything Northwind-specific is catalog data in
`src/secure_query/evals/suites/northwind/catalog.json`; no code knows about it.

```bash
python -m secure_query.demo.load_northwind   # pinned commit + SHA-256
python -m secure_query.evals.run --provider openai \
  --suite src/secure_query/evals/suites/northwind/cases.json
```

Dev cases for Northwind live in `suites/northwind_dev/` (`--suite northwind_dev`).
`cases.json` (36 cases: 28 answer, 8 abstain) was frozen before the first live
run. Treat the whole file as a holdout: never tune prompts, guards or the
Northwind catalog against its failures. To work on a failure mode it exposed,
write new dev cases (on either schema) that show it, fix against those, then
re-run Northwind once.

## Head-to-head with Genie

`python -m secure_query.evals.scorecard --suite <cases.json> --repeat 5 --out <file>`
scores Secure Query and Genie on the same cases with the **same matcher** (column order,
float noise and LIMIT ties tolerated for both). A Genie reply with no query result, such as
a clarifying question, counts as a refusal. Genie cells are converted to their declared
column types before comparison. `--sq-only` skips Genie. Until PLAN task 2.1 lands, the
references and Secure Query run on the local DuckDB file, so a live comparison needs the
same data in the warehouse.

## Qualifying a new model

Any model on an OpenAI-compatible endpoint runs (`SECURE_QUERY_MODEL`, and
`SECURE_QUERY_BASE_URL` for a non-OpenAI endpoint). Running is not the same as trusted:
wrong-rate is a property of the model, so each model is measured before it is used.

1. Run the dev suites with the candidate and with the current default, same day:
   ```bash
   python -m secure_query.evals.run --split dev --provider openai --model <candidate> --json dev-<candidate>.json
   python -m secure_query.evals.run --suite src/secure_query/evals/suites/northwind_dev/cases.json --provider openai --model <candidate>
   ```
2. Read the log for `rejected 'temperature'`. If the model refuses a temperature, both
   readings of the self-consistency gate sample at the model's default, and the first
   reading is no longer deterministic. Record that next to the numbers. A reasoning model
   may accept a temperature with `SECURE_QUERY_REASONING_EFFORT=none`.
3. Compare wrong answers first, then answer-rate, seconds per question and price. A
   candidate qualifies for dev when its wrong count is not above the current default's.
4. Before it becomes the default or goes to a client: run the release gate below and the
   domain holdout with it, and adjudicate. Record the model id in the baseline file.

## Release gate

- Adjudicated holdout wrong-rate: 95% upper bound ≤ 2% (PLAN.md Goal).
- Dev suite ≥ 100 questions.
- Golden SQL tests pass (`pytest`).

CI runs the mock holdout on both planner paths. It checks wiring, not quality. A
live-model holdout is a laptop or nightly job.

## Adding cases

1. Add to `dev_expansion.py` or `suites/chinook/cases.json` (non-holdout id only)
2. `reference_sql` for answer cases; `reason` for abstain cases. Run the reference against the data first: a reference that returns 0 rows or 0 (e.g. a year outside the data's 2021–2025 range) makes the case pass trivially
3. Tag with `intent`, `pii`, `table-selection`, etc.
4. Never add holdout ids in the same commit as prompt/guard tuning

## Baselines

Save runs under `docs/baselines/` with the date and model id in the file name. The current result is in [STATUS.md](STATUS.md).
