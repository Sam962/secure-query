# Eval protocol

## Splits

| Split | Purpose | Rule |
|-------|---------|------|
| **dev** | Tune prompts, guards, synonyms | OK to iterate |
| **holdout** | Release gate | **Never tune on holdout** |
| **all** | Full picture only | Do not use for tuning |

Holdout case ids are frozen in `secure_query/evals/dev_expansion.py` (`HOLDOUT_CASE_IDS`).

## Commands

```bash
pytest secure_query/tests/test_suite_split.py -q
python -m secure_query.evals.run_chinook --accuracy --split dev --provider mock
python -m secure_query.evals.run_chinook --accuracy --split holdout --fail-on-wrong --provider ollama
```

## Release gate

- **holdout wrong-rate = 0** (wrong + unsafe)
- dev suite ≥ 100 questions
- golden SQL tests pass (`pytest`)

## Adding cases

1. Add to `dev_expansion.py` or legacy `chinook_live.json` (non-holdout id only)
2. Include `reference_sql` for answer cases; `reason` for abstain cases
3. Tag with `intent`, `pii`, `table-selection`, etc.
4. Never add holdout ids to dev tuning commits

## Baselines

Save holdout runs under `docs/baselines/holdout-YYYY-MM-DD.txt` when measuring progress.
