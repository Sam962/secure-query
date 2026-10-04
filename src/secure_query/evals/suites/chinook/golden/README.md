# Chinook compiler goldens

Each subdirectory is one case:

- `case.json` — question, structural checks, optional expected rows / error codes
- `plan.json` — approved LogicalPlan fixture
- `expected.sql` — exact compiler output (positive cases only)

These protect the **compiler**, not the live planner. Execution accuracy (NL → rows vs reference SQL) is:

```bash
python -m secure_query.evals.run --split holdout --provider ollama
```

Goldens only:

```bash
pytest tests/test_chinook_evals.py -q
```
