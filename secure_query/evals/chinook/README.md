# Chinook eval fixtures (Phase 4)

Each subdirectory is one case:

- `case.json` — question, structural checks, optional expected rows / error codes
- `plan.json` — approved LogicalPlan fixture
- `expected.sql` — exact compiler output (positive cases only)

Run:

```bash
pytest secure_query/tests/test_chinook_evals.py -q
python -m secure_query.evals.run_chinook
python -m secure_query.evals.run_chinook --live --provider ollama
```
