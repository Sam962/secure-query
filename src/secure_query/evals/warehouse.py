"""Databricks helpers for head-to-head suites: statements, gold SQL conversion, verification.

A question is kept only when its gold SQL, converted from SQLite to Databricks, returns the
same rows on the warehouse as on SQLite. Three kinds are left out because exact match cannot
grade them: an answer larger than the smallest row cap of the systems compared, a gold query
that selects a column its GROUP BY does not determine (SQLite picks an arbitrary row), and a
gold LIMIT that cuts through a tie (a system that returns every tied row is not wrong).
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Any

import sqlglot
from sqlglot import exp

ROW_CAP = 5_000  # the Genie API returns at most this many rows


def settings() -> tuple[str, str, str]:
    """(host, token, warehouse id) from the environment."""
    host = os.environ["DATABRICKS_HOST"].rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    return host, os.environ["DATABRICKS_TOKEN"], os.environ["DATABRICKS_HTTP_PATH"].rsplit("/", 1)[-1]


def call(method: str, path: str, body: Any = None, *, data: bytes | None = None) -> dict:
    host, token, _ = settings()
    headers = {"Authorization": "Bearer " + token}
    if data is None and body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(host + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:400]}") from None
    return json.loads(raw) if raw else {}


def statement(sql: str, *, catalog: str | None = None, schema: str | None = None) -> tuple[list | None, str | None]:
    """Run one statement; (rows, None) or (None, error). A result over one chunk is an error."""
    body = {"warehouse_id": settings()[2], "statement": sql, "wait_timeout": "50s"}
    if catalog:
        body["catalog"] = catalog
    if schema:
        body["schema"] = schema
    try:
        r = call("POST", "/api/2.0/sql/statements", body)
    except RuntimeError as e:
        return None, str(e)[:250]
    if r.get("status", {}).get("state") != "SUCCEEDED":
        return None, r.get("status", {}).get("error", {}).get("message", str(r.get("status")))[:250]
    if r.get("manifest", {}).get("truncated") or r.get("result", {}).get("next_chunk_index"):
        return None, "result too large to compare in one chunk"
    return r.get("result", {}).get("data_array", []), None


def convert(sql: str, columns: set[str]) -> str:
    """SQLite gold SQL as Databricks SQL with SQLite's meaning kept."""
    tree = sqlglot.parse_one(sql, read="sqlite")
    for col in list(tree.find_all(exp.Column)):  # SQLite reads "text" as a string when no column has that name
        ident = col.this
        if isinstance(ident, exp.Identifier) and ident.quoted and not col.table and ident.name.lower() not in columns:
            col.replace(exp.Literal.string(ident.name))
    for like in list(tree.find_all(exp.Like)):  # SQLite LIKE ignores case
        like.replace(exp.ILike(this=like.this, expression=like.expression))
    for agg in list(tree.find_all(exp.Sum, exp.Avg)):  # SQLite sums text as numbers ('' counts 0)
        if not isinstance(agg.this, exp.Distinct):
            agg.this.replace(exp.TryCast(this=agg.this.copy(), to=exp.DataType.build("double")))
    for select in list(tree.find_all(exp.Select)):  # SQLite lets a grouped query select a bare column
        group = select.args.get("group")
        if not group:
            continue
        keys = {g.sql().lower() for g in group.expressions}
        for proj in select.expressions:
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.Column) and inner.sql().lower() not in keys:
                inner.replace(exp.AnyValue(this=inner.copy()))
    return tree.sql(dialect="databricks")


def norm(rows: list) -> list[tuple]:
    out = []
    for r in rows:
        t = []
        for v in r:
            try:
                t.append(round(float(v), 4))
            except (TypeError, ValueError):
                t.append(v)
        out.append(tuple(t))
    return sorted(out, key=repr)


def verified(gold_sql: str, sqlite_file: str, columns: set[str], run=statement) -> tuple[str | None, str]:
    """(Databricks SQL, "") when it is gradable and returns the gold's rows; else (None, why)."""
    gold = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    gold.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        local = norm(gold.execute(gold_sql).fetchall())
    finally:
        gold.close()
    try:
        sql = convert(gold_sql, columns)
    except Exception as e:  # noqa: BLE001 - any parse failure leaves the question out
        return None, "convert: " + repr(e)[:150]
    rows, err = run(sql)
    if err:
        return None, err
    if norm(rows) != local:
        return None, f"rows differ: sqlite {len(local)} {local[:2]} | warehouse {len(rows)} {norm(rows)[:2]}"
    if len(rows) > ROW_CAP:
        return None, f"answer has {len(rows)} rows, over the {ROW_CAP}-row cap"
    if _cuts_a_tie(sql, run):
        return None, "gold LIMIT cuts through a tie"
    if "ANY_VALUE(" in sql:  # the bare column is determined by the group only if its lowest and highest agree
        low, high = (run(sql.replace("ANY_VALUE(", f + "("))[0] for f in ("MIN", "MAX"))
        if low is None or low != high:
            return None, "gold selects a column its GROUP BY does not determine"
    return sql, ""


def _cuts_a_tie(sql: str, run) -> bool:
    """True when the row after the gold's LIMIT has the same sort key as the last row inside it."""
    tree = sqlglot.parse_one(sql, read="databricks")
    order, limit = tree.args.get("order"), tree.args.get("limit")
    if not isinstance(tree, exp.Select) or not order or not limit or not limit.expression.is_int:
        return False
    n = int(limit.expression.name)
    probe = tree.copy().select(*[o.this.copy() for o in order.expressions], append=False).limit(n + 1)
    rows, _ = run(probe.sql(dialect="databricks"))
    return bool(rows) and len(rows) == n + 1 and rows[-1] == rows[-2]
