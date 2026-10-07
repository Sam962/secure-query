# Domain ownership (template)

Fill this in before a real warehouse domain goes live. Catalogs without a named
owner are not production. How to run the demo and draft a catalog: root [README](../README.md).

| Domain | Catalog owner | Metric approver | Eval holdout owner | Schema-change SLA |
|--------|---------------|-----------------|--------------------|-------------------|
| chinook (demo) | engineering | engineering | engineering | n/a |

## Release policy

- Holdout **wrong-rate = 0** is the gate. Coverage/accuracy may lag.
- Do not tune prompts or guards on holdout case ids.
- A new table or metric is not queryable until a catalog PR merges.

## RACI (copy per domain)

| Activity | Responsible | Accountable | Consulted | Informed |
|----------|-------------|-------------|-----------|----------|
| Approve tables / PII flags | | | | |
| Approve metric definitions | | | | |
| Run holdout on catalog change | | | | |
| Respond to schema drift | | | | |
