"""One Spider test database as a suite the head-to-head scorecard can run on Databricks.

The gold SQL is converted from SQLite to Databricks and checked (evals.warehouse.verified). The tables must already
be in SECURE_QUERY_DATABRICKS_SCHEMA (see .env.example). The catalog gets value lists for
its category columns (engine.catalog_draft.with_value_lists).

Usage:
    python scripts/spider_warehouse_suite.py aan_1 data/h2h/aan_1
    python -m secure_query.evals.scorecard --suite data/h2h/aan_1/cases.json --backend databricks --repeat 3 --out OUT.json
"""
import json
import os
import sys
from pathlib import Path

import duckdb

from secure_query.engine.catalog_draft import with_value_lists
from secure_query.engine.env import load_dotenv
from secure_query.evals.spider import load_spider
from secure_query.evals.warehouse import statement, verified

load_dotenv()
DB = sys.argv[1]; here = Path(sys.argv[2]); here.mkdir(parents=True, exist_ok=True)
cat, sch = os.environ["SECURE_QUERY_DATABRICKS_SCHEMA"].split(".")

def warehouse(q): return statement(q, catalog=cat, schema=sch)

items = [(c, catalog, db) for c, catalog, db in load_spider("test") if f"db:{DB}" in c.tags]
local = duckdb.connect(str(items[0][2]), read_only=True)  # the same rows as the warehouse tables
catalog = with_value_lists(items[0][1], lambda q: local.execute(q).fetchall())  # stands in for the owner's review
local.close()
columns = {col.name.lower() for t in catalog.tables for col in t.columns}
cases, bad = [], []
for case, _, _ in items:
    sql, why = verified(case.reference_sql, case.reference_db, columns, warehouse)
    if sql is None: bad.append((case.case_id, why, case.reference_sql)); continue
    cases.append({"id": case.case_id, "question": case.question, "expect": "answer", "reference_sql": sql,
                  "tags": list(case.tags), "extra_columns_ok": True})
(here / "catalog.json").write_text(catalog.model_dump_json())
(here / "cases.json").write_text(json.dumps({"catalog": "catalog.json", "notes": f"Spider test {DB}, gold SQL converted to Databricks and checked against SQLite rows", "cases": cases}, indent=1))
print(f"{len(items)} questions loaded; {len(cases)} converted and verified; {len(bad)} not")
for b in bad: print(" -", b[0], "|", b[1], "\n     ", b[2][:230])
