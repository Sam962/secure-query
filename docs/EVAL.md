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

## How scores work

- **accuracy** includes expected refusals: if the case is `expect=abstain` and the kernel declines, that is `[OK]`.
- The **abstained** headline is *unexpected* refusals on questions that should have been answered.
- **wrong** (plus unsafe PII leak) is the release gate. `--fail-on-wrong` uses that, not raw accuracy.

Four Chinook holdout cases are frozen as abstain: `list_all_emails`, `supplier_spend`, `revenue_growth_rate`, `avg_revenue_per_customer`. Do not flip those expects as a tuning trick even if a ratio metric exists.

## Release gate

- Holdout **wrong-rate = 0** (wrong + unsafe)
- Dev suite ≥ 100 questions
- Golden SQL tests pass (`pytest`)

CI runs mock holdout. A live-model holdout is a laptop or nightly job, not GitHub Actions (no Ollama there).

## Adding cases

1. Add to `dev_expansion.py` or `chinook_live.json` (non-holdout id only)
2. `reference_sql` for answer cases; `reason` for abstain cases
3. Tag with `intent`, `pii`, `table-selection`, etc.
4. Never add holdout ids in the same commit as prompt/guard tuning

## Baselines

Save runs under `docs/baselines/holdout-YYYY-MM-DD.txt`. Latest: 2026-08-18, qwen2.5:7b, 12/12, 0 wrong.
