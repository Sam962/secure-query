"""One KaggleDBQA database on Databricks: tables, a Genie space, and a head-to-head suite.

The stand-in for a customer domain: real Kaggle data with its owners' documentation.
Both systems get the same owner knowledge:
  - the documentation (table and column descriptions, code meanings, the overview), which
    Secure Query reads from its catalog and Genie from the space's descriptions and its
    text instruction;
  - the fewshot questions with their SQL, as Secure Query certified answers and as Genie
    example question SQL (the questions the owner already knows people ask).
The test questions, never tuned on, are the suite. Gold SQL is converted and verified
(evals.warehouse.verified). Writes <out>/catalog.json and <out>/cases.json and prints the
settings a scorecard run needs.

Usage:
    python scripts/kaggledbqa_warehouse.py GeoNuclearData data/h2h/kaggledbqa/GeoNuclearData
"""
import json
import os
import sys
import uuid
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from secure_query.engine.catalog_draft import with_value_lists
from secure_query.engine.env import load_dotenv
from secure_query.evals.kaggledbqa import load_kaggledbqa
from secure_query.evals.warehouse import call, settings, statement, verified
from secure_query.kernel.catalog import CertifiedQuery
from secure_query.kernel.sql_validate import SqlValidationFailed, validate_sql

load_dotenv()
DB = sys.argv[1]; here = Path(sys.argv[2]); here.mkdir(parents=True, exist_ok=True)
cat = os.environ["SECURE_QUERY_DATABRICKS_SCHEMA"].split(".")[0]
sch = f"kaggledbqa_{DB.lower()}"
tmp = Path(os.environ.get("CLAUDE_JOB_DIR", "/tmp")) / "tmp" / sch
tmp.mkdir(parents=True, exist_ok=True)


def run(q):
    rows, err = statement(q, catalog=cat, schema=sch)
    if err:
        sys.exit(f"{q[:120]}: {err}")
    return rows


def warehouse(q): return statement(q, catalog=cat, schema=sch)


test = [i for i in load_kaggledbqa("test", docs=True) if i[1].tenant_id == f"kaggledbqa_{DB}"]
fewshot = [i for i in load_kaggledbqa("fewshot", docs=True) if i[1].tenant_id == f"kaggledbqa_{DB}"]
duck = test[0][2]
local = duckdb.connect(str(duck), read_only=True)
catalog = with_value_lists(test[0][1], lambda q: local.execute(q).fetchall()).model_copy(update={"sql_dialect": "databricks"})

# Tables: each as Parquet through a volume, then a Delta table.
run(f"CREATE SCHEMA IF NOT EXISTS {cat}.{sch}")
run(f"CREATE VOLUME IF NOT EXISTS {cat}.{sch}.load")
for table in catalog.tables:
    target = f"{cat}.{sch}.`{table.name.lower()}`"
    if os.environ.get("SKIP_LOAD"):
        break
    pq = tmp / f"{table.name}.parquet"
    local.execute(f"COPY \"{table.name}\" TO '{pq}' (FORMAT parquet)")
    path = f"/Volumes/{cat}/{sch}/load/{table.name}.parquet"
    call("PUT", f"/api/2.0/fs/files{path}?overwrite=true", data=pq.read_bytes())
    run(f"CREATE OR REPLACE TABLE {target} TBLPROPERTIES ('delta.columnMapping.mode' = 'name') "
        f"AS SELECT * FROM read_files('{path}', format => 'parquet')")
    pq.unlink()
    print(f"{target}: {run(f'SELECT COUNT(*) FROM {target}')[0][0]} rows")
local.close()

columns = {c.name.lower() for t in catalog.tables for c in t.columns}

# The owner's known questions: certified when the SQL verifies and passes the kernel.
certified, skipped = [], []
for case, _, _ in fewshot:
    sql, why = verified(case.reference_sql, case.reference_db, columns, warehouse)
    if sql is not None:
        try:
            validate_sql(sql, catalog)
        except SqlValidationFailed as e:
            sql, why = None, "kernel: " + "; ".join(x.code for x in e.errors)
    if sql is None:
        skipped.append((case.case_id, why)); continue
    certified.append(CertifiedQuery(id=f"k{len(certified) + 1:02d}", question=case.question, sql=sql))
catalog = catalog.model_copy(update={"certified": certified})

cases, bad = [], []
for case, _, _ in test:
    sql, why = verified(case.reference_sql, case.reference_db, columns, warehouse)
    if sql is None: bad.append((case.case_id, why, case.reference_sql)); continue
    cases.append({"id": case.case_id, "question": case.question, "expect": "answer", "reference_sql": sql,
                  "tags": list(case.tags), "extra_columns_ok": True})


# Genie space: the same documentation, joins, value matching and known questions.
def ident(table): return f"{cat}.{sch}.{table.lower()}"
def text(s): return {"description": [s]} if s else {}
def qualified(sql):
    tree = sqlglot.parse_one(sql, read="databricks")
    for t in tree.find_all(exp.Table):
        if not t.args.get("db") and t.name.lower() not in {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}:
            t.set("db", exp.to_identifier(sch)); t.set("catalog", exp.to_identifier(cat))
    return tree.sql(dialect="databricks")
def sid(): return uuid.uuid4().hex

tables = sorted(({"identifier": ident(t.name), **text(t.description), "column_configs": sorted(
    ({"column_name": c.name, **text(c.description), **({"enable_entity_matching": True} if c.values else {})}
     for c in t.columns), key=lambda c: c["column_name"])} for t in catalog.tables), key=lambda t: t["identifier"])
instructions = {
    "join_specs": sorted(({"id": sid(), "left": {"identifier": ident(j.left_table)}, "right": {"identifier": ident(j.right_table)},
                           "sql": [f"{ident(j.left_table)}.{j.left_column} = {ident(j.right_table)}.{j.right_column}",
                                   "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--"]} for j in catalog.join_keys), key=lambda j: j["id"]),
    "example_question_sqls": sorted(({"id": sid(), "question": [c.question], "sql": [qualified(c.sql)]} for c in certified),
                                    key=lambda e: e["id"]),
}
if catalog.instructions:
    instructions["text_instructions"] = [{"id": sid(), "content": ["\n".join(catalog.instructions)]}]
space = call("POST", "/api/2.0/genie/spaces", {"title": f"KaggleDBQA {DB}", "warehouse_id": settings()[2],
    "description": f"KaggleDBQA {DB}: the owners' documentation, joins, and the fewshot questions as examples.",
    "serialized_space": json.dumps({"version": 2, "data_sources": {"tables": tables}, "instructions": instructions})})

(here / "catalog.json").write_text(catalog.model_dump_json())
(here / "cases.json").write_text(json.dumps({"catalog": "catalog.json", "notes": f"KaggleDBQA test {DB}, gold SQL converted to Databricks and verified; fewshot questions certified", "cases": cases}, indent=1))
print(f"{len(test)} test questions; {len(cases)} verified; {len(certified)} of {len(fewshot)} fewshot certified")
for b in bad: print(" - test", b[0], "|", b[1][:160])
for s in skipped: print(" - fewshot", s[0], "|", s[1][:160])
print(f"SECURE_QUERY_DATABRICKS_SCHEMA={cat}.{sch}\nGENIE_SPACE_ID={space['space_id']}")
