"""Load one Spider test database into Databricks and make a Genie space for it.

Creates schema <catalog>.<db> (catalog from SECURE_QUERY_DATABRICKS_SCHEMA), one table per
SQLite table with lowercase names, and a Genie space that holds what the Secure Query
catalog holds and nothing more: the tables, the column descriptions and the joins. No
instructions and no example questions. Needs a token that may create schemas, tables and
Genie spaces. Prints the two settings a scorecard run on this database needs.

Usage:
    python scripts/spider_warehouse_load.py bakery_1
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid

from secure_query.engine.env import load_dotenv
from secure_query.evals.spider import load_spider

load_dotenv()
DB = sys.argv[1]
host = os.environ["DATABRICKS_HOST"].rstrip("/"); tok = os.environ["DATABRICKS_TOKEN"]
wh = os.environ["DATABRICKS_HTTP_PATH"].rsplit("/", 1)[-1]
cat = os.environ["SECURE_QUERY_DATABRICKS_SCHEMA"].split(".")[0]
schema = f"{cat}.{DB.lower()}"

def call(method, path, body=None):
    req = urllib.request.Request(host + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e: sys.exit(f"{method} {path}: {e.code} {e.read().decode()[:400]}")

def run(statement):
    r = call("POST", "/api/2.0/sql/statements", {"warehouse_id": wh, "statement": statement, "wait_timeout": "50s"})
    if r.get("status", {}).get("state") != "SUCCEEDED": sys.exit(f"{statement[:120]}: {r.get('status')}")

def literal(v):
    if v is None: return "NULL"
    if isinstance(v, (int, float)): return repr(v)
    if isinstance(v, bytes): v = v.decode("utf-8", "replace")
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"

case, catalog = next((c, k) for c, k, _ in load_spider("test") if f"db:{DB}" in c.tags)
src = sqlite3.connect(f"file:{case.reference_db}?mode=ro", uri=True)
src.text_factory = lambda b: b.decode("utf-8", "replace")
run(f"CREATE SCHEMA IF NOT EXISTS {schema}")
for table in catalog.tables:
    cur = src.execute(f'SELECT * FROM "{table.name}"')
    names = [d[0] for d in cur.description]; rows = cur.fetchall()
    types = []
    for i in range(len(names)):  # a column's type is what its values are: SQLite's declared types are loose
        kinds = {type(r[i]) for r in rows if r[i] is not None}
        types.append("BIGINT" if kinds == {int} else "DOUBLE" if kinds and kinds <= {int, float} else "STRING")
    rows = [tuple(str(v) if t == "STRING" and isinstance(v, (int, float)) else v for v, t in zip(r, types)) for r in rows]
    target = f"{schema}.`{table.name.lower()}`"
    run(f"CREATE OR REPLACE TABLE {target} (" + ", ".join(f"`{n}` {t}" for n, t in zip(names, types)) + ")")
    for i in range(0, len(rows), 500):
        run(f"INSERT INTO {target} VALUES " + ", ".join("(" + ", ".join(map(literal, r)) + ")" for r in rows[i:i + 500]))
    print(f"{target}: {len(rows)} rows")

def ident(table): return f"{schema}.{table.lower()}"
def text(s): return {"description": [s]} if s else {}
tables = sorted(({"identifier": ident(t.name), **text(t.description), "column_configs": sorted(
    ({"column_name": c.name, **text(c.description)} for c in t.columns), key=lambda c: c["column_name"])}
    for t in catalog.tables), key=lambda t: t["identifier"])
joins = sorted(({"id": uuid.uuid4().hex, "left": {"identifier": ident(j.left_table)}, "right": {"identifier": ident(j.right_table)},
                 "sql": [f"{ident(j.left_table)}.{j.left_column} = {ident(j.right_table)}.{j.right_column}",
                         "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--"]}  # a foreign key to the table it points at
                for j in catalog.join_keys), key=lambda j: j["id"])
space = call("POST", "/api/2.0/genie/spaces", {"title": f"Spider {DB}", "warehouse_id": wh,
    "description": f"Spider test database {DB}. Tables, column descriptions and joins only.",
    "serialized_space": json.dumps({"version": 2, "data_sources": {"tables": tables}, "instructions": {"join_specs": joins}})})
print(f"SECURE_QUERY_DATABRICKS_SCHEMA={schema}\nGENIE_SPACE_ID={space['space_id']}")
