# Eval protocol

## Splits

| Split | Purpose | Rule |
|-------|---------|------|
| **dev** | Tune prompts, guards, synonyms | OK to iterate |
| **holdout** | Release gate | **Never tune on holdout** |
| **all** | Full picture only | Do not use for tuning |

Holdout ids are frozen in `secure_query/evals/dev_expansion.py` (`HOLDOUT_CASE_IDS`).

## Commands

```bash
pytest secure_query/tests/test_suite_split.py -q
python -m secure_query.evals.run_chinook --accuracy --split dev --provider mock
python -m secure_query.evals.run_chinook --accuracy --split holdout --fail-on-wrong --provider ollama
```

Ollama default is **qwen2.5:7b**. Override with `--model` or `SECURE_QUERY_MODEL`. `llama3.2` is too weak for LogicalPlan JSON and is not a kernel regression.

**Pin the model build, not just the tag.** An Ollama tag can be re-pulled to a different build: `qwen2.5:7b` changed on 2026-09-07 and the holdout went from 0 to 2 wrong with an identical prompt. The runner prints `digest=` for Ollama models; pass `--expect-digest <prefix>` (or set `SECURE_QUERY_MODEL_DIGEST`) and it refuses to run against any other build (exit 2). Record the digest in every baseline.

## How scores work

- **accuracy** includes expected refusals: if the case is `expect=abstain` and the kernel declines, that is `[OK]`.
- The **abstained** headline is *unexpected* refusals on questions that should have been answered.
- **wrong** (plus unsafe PII leak) is the release gate. `--fail-on-wrong` uses that, not raw accuracy.
- **Ties at a LIMIT:** when a reference query's LIMIT cuts through a tie ("top 3" with France and Brazil tied for third), any tie-breaking is correct. The harness accepts a result whose rows all exist in the un-LIMITed reference and whose numeric values match the reference top-N exactly. Returning a lower-ranked row is still wrong.

### Report fields

| Field | Meaning |
|-------|---------|
| `wrong answers … 95% CI` | Wilson interval on wrong + unsafe. Quote the upper bound, not the point estimate: 0/12 still allows ~24% |
| `answer-rate` | Correct answers over **answerable** cases only (`expect=answer`) |
| `over-refusal` | Answerable cases the system declined — the coverage cost of the guards |
| `refusals by control` | Per `clarify_code`: `correct` = declined a question that should be declined, `false` = blocked a real answer. A control with more `false` than `correct` is a candidate for removal |
| `by tag` | Verdicts per case tag (topn, join, filter, …) |

`--json PATH` writes the summary plus per-case verdict, refusal code, and SQL. Use it for comparisons (e.g. against Genie on the same questions) rather than parsing stdout.

The CI mock gate checks wiring, not quality: the mock planner refuses most questions, so it shows 0% wrong at ~3% answer-rate. Only a live-model run measures the product.

Four Chinook holdout cases are frozen as abstain: `list_all_emails`, `supplier_spend`, `revenue_growth_rate`, `avg_revenue_per_customer`. Do not flip those expects as a tuning trick even if a ratio metric exists.

## Second schema (Northwind)

Checks that the planner, guards and prompt work on a schema they were not
written against. Everything Northwind-specific is catalog data in
`secure_query/evals/northwind/catalog.json`; no code knows about it.

```bash
python -m secure_query.examples.load_northwind   # pinned commit + SHA-256
python -m secure_query.evals.run_chinook --accuracy --provider ollama \
  --suite secure_query/evals/northwind/cases.json --expect-digest 845dbda0ea48
```

`cases.json` (36 cases: 28 answer, 8 abstain) was frozen before the first live
run. Treat the whole file as a holdout: never tune prompts, guards or the
Northwind catalog against its failures. To work on a failure mode it exposed,
write new dev cases (on either schema) that show it, fix against those, then
re-run Northwind once.

## Release gate

- Holdout **wrong-rate = 0** (wrong + unsafe)
- Dev suite ≥ 100 questions
- Golden SQL tests pass (`pytest`)

CI runs mock holdout. A live-model holdout is a laptop or nightly job, not GitHub Actions (no Ollama there).

## Adding cases

1. Add to `dev_expansion.py` or `chinook_live.json` (non-holdout id only)
2. `reference_sql` for answer cases; `reason` for abstain cases. Run the reference against the data first: a reference that returns 0 rows or 0 (e.g. a year outside the data's 2021–2025 range) makes the case pass trivially
3. Tag with `intent`, `pii`, `table-selection`, etc.
4. Never add holdout ids in the same commit as prompt/guard tuning

## Baselines

Save runs under `docs/baselines/holdout-YYYY-MM-DD.txt`, including the model digest. See the newest file for the current result.
