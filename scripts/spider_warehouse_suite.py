"""One Spider test database as a suite the head-to-head scorecard can run on Databricks.

The gold SQL is converted from SQLite to Databricks, and a question is kept only when the
converted query returns the same rows on the warehouse as the gold does on SQLite. The
tables must already be in SECURE_QUERY_DATABRICKS_SCHEMA (see .env.example).

Usage:
    python scripts/spider_warehouse_suite.py aan_1 data/h2h/aan_1
    python -m secure_query.evals.scorecard --suite data/h2h/aan_1/cases.json --backend databricks --repeat 3 --out OUT.json
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

import sqlglot
from sqlglot import exp

from secure_query.engine.env import load_dotenv
from secure_query.evals.spider import load_spider

load_dotenv()
DB = sys.argv[1]; here = Path(sys.argv[2]); here.mkdir(parents=True, exist_ok=True)
host = os.environ["DATABRICKS_HOST"].rstrip("/"); tok = os.environ["DATABRICKS_TOKEN"]
wh = os.environ["DATABRICKS_HTTP_PATH"].rsplit("/", 1)[-1]
cat, sch = os.environ["SECURE_QUERY_DATABRICKS_SCHEMA"].split(".")

def warehouse(q):
    req = urllib.request.Request(host + "/api/2.0/sql/statements", headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        data=json.dumps({"warehouse_id": wh, "statement": q, "catalog": cat, "schema": sch, "wait_timeout": "50s"}).encode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r: r = json.loads(r.read())
    except urllib.error.HTTPError as e: return None, e.read().decode()[:200]
    if r.get("status", {}).get("state") != "SUCCEEDED": return None, r.get("status", {}).get("error", {}).get("message", "")[:250]
    if r.get("manifest", {}).get("truncated") or r.get("result", {}).get("next_chunk_index"): return None, "result too large to compare in one chunk"
    return r["result"].get("data_array", []), None

def convert(sql, columns):
    tree = sqlglot.parse_one(sql, read="sqlite")
    for col in list(tree.find_all(exp.Column)):  # SQLite reads "text" as a string when no column has that name
        ident = col.this
        if isinstance(ident, exp.Identifier) and ident.quoted and not col.table and ident.name.lower() not in columns:
            col.replace(exp.Literal.string(ident.name))
    for like in list(tree.find_all(exp.Like)):  # SQLite LIKE ignores case
        like.replace(exp.ILike(this=like.this, expression=like.expression))
    for select in list(tree.find_all(exp.Select)):  # SQLite lets a grouped query select a bare column
        group = select.args.get("group")
        if not group: continue
        keys = {g.sql().lower() for g in group.expressions}
        for i, proj in enumerate(select.expressions):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Column) and inner.sql().lower() not in keys:
                inner.replace(exp.AnyValue(this=inner.copy()))
    return tree.sql(dialect="databricks")

def norm(rows):
    out = []
    for r in rows:
        t = []
        for v in r:
            try: t.append(round(float(v), 4))
            except (TypeError, ValueError): t.append(v)
        out.append(tuple(t))
    return sorted(out, key=repr)

items = [(c, catalog) for c, catalog, _ in load_spider("test") if f"db:{DB}" in c.tags]
catalog = items[0][1]
columns = {col.name.lower() for t in catalog.tables for col in t.columns}
cases, bad = [], []
for case, _ in items:
    gold = sqlite3.connect(f"file:{case.reference_db}?mode=ro", uri=True)
    gold.text_factory = lambda b: b.decode("utf-8", "replace")
    local = norm(gold.execute(case.reference_sql).fetchall()); gold.close()
    try: sql = convert(case.reference_sql, columns)
    except Exception as e: bad.append((case.case_id, "convert: " + repr(e)[:150], case.reference_sql)); continue
    rows, err = warehouse(sql)
    if err: bad.append((case.case_id, err, sql)); continue
    if norm(rows) != local:
        bad.append((case.case_id, f"rows differ: sqlite {len(local)} {local[:2]} | warehouse {len(rows)} {norm(rows)[:2]}", sql)); continue
    cases.append({"id": case.case_id, "question": case.question, "expect": "answer", "reference_sql": sql,
                  "tags": list(case.tags), "extra_columns_ok": True})
(here / "catalog.json").write_text(catalog.model_dump_json())
(here / "cases.json").write_text(json.dumps({"catalog": "catalog.json", "notes": f"Spider test {DB}, gold SQL converted to Databricks and checked against SQLite rows", "cases": cases}, indent=1))
print(f"{len(items)} questions loaded; {len(cases)} converted and verified; {len(bad)} not")
for b in bad: print(" -", b[0], "|", b[1], "\n     ", b[2][:230])
