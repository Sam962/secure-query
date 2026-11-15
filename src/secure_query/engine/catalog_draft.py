"""Draft an unapproved Catalog from DuckDB information_schema.

A data owner must review PII flags and join keys before the draft is the
planner allowlist. This is a toil reducer, not an auto-approval pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlglot import exp

from secure_query.engine.databricks import draft_catalog, fetch_unity_catalog_draft, map_uc_dtype
from secure_query.kernel.catalog import Catalog, JoinKey


def draft_from_duckdb(db_path: str | Path, *, tenant_id: str) -> dict:
    import duckdb

    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    con = duckdb.connect(str(path), read_only=True)
    try:
        col_rows = con.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
    finally:
        con.close()

    columns = [
        {
            "table_name": r[0],
            "column_name": r[1],
            "data_type": r[2],
            "mapped": map_uc_dtype(str(r[2])),
        }
        for r in col_rows
    ]
    catalog = draft_catalog(
        tenant_id=tenant_id,
        columns=columns,
        foreign_keys=[],
    )
    catalog = catalog.model_copy(update={"sql_dialect": "duckdb"})
    return json.loads(catalog.model_dump_json())


MAX_VALUES = 25
MAX_VALUE_LENGTH = 40


def with_value_lists(catalog: Catalog, run: Callable[[str], Sequence[Sequence[Any]]]) -> Catalog:
    """`catalog` with `values` drafted for its category columns, for the owner to review.

    A category column is a non-PII string column with at most MAX_VALUES distinct short
    values, each stored twice on average: a set of codes, not names of people or things.
    `run` executes one read-only query in the catalog's dialect and returns its rows.
    """
    tables = []
    for table in catalog.tables:
        columns = []
        for col in table.columns:
            if col.dtype == "str" and col.pii_risk == "none":
                ident = exp.column(col.name, quoted=True)
                query = (
                    exp.select(ident, exp.Count(this=exp.Star()))
                    .from_(exp.table_(table.name, quoted=True))
                    .where(ident.copy().is_(exp.null()).not_())
                    .group_by(ident.copy())
                    .limit(MAX_VALUES + 1)
                )
                rows = [r for r in run(query.sql(dialect=catalog.sql_dialect)) if isinstance(r[0], str)]
                if (
                    0 < len(rows) <= MAX_VALUES
                    and sum(r[1] for r in rows) >= 2 * len(rows)
                    and all(0 < len(r[0]) <= MAX_VALUE_LENGTH for r in rows)
                ):
                    col = col.model_copy(update={"values": tuple(sorted(r[0] for r in rows))})
            columns.append(col)
        tables.append(table.model_copy(update={"columns": columns}))
    return catalog.model_copy(update={"tables": tables})


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _names_link(column: str, table: str, key: str) -> bool:
    """Does `column`'s name say it refers to `table`.`key`? (orders.customer_id -> customers.id)"""
    col, key = _norm(column), _norm(key)
    tab = _norm(table)
    stems = {tab, tab.removesuffix("s"), tab[:-3] + "y" if tab.endswith("ies") else tab}
    if col == key and key not in ("id", "name", "code", "type"):
        return True  # the same key name in both tables
    if any(col == s + key for s in stems) or (key in ("id", "code", "uid") and col in stems):
        return True  # the table's name, then the key: customer_id, customerid, or just customer
    prefix = col.removesuffix(key)
    return prefix != col and bool(prefix) and any(s.startswith(prefix) for s in stems)  # cust_id


def with_discovered_joins(catalog: Catalog, run: Callable[[str], Sequence[Sequence[Any]]]) -> Catalog:
    """`catalog` with the joins the data shows but the schema does not declare, for the owner to review.

    Warehouses often declare no foreign keys (Unity Catalog rarely does), and a catalog without
    joins refuses every question that needs one. A join A.x -> B.y is proposed when B.y is a
    key (no nulls, no repeats), A.x and B.y are both text or both not, A.x holds at least two
    values and every one of them is stored in B.y, and the names agree (`_names_link`). Reads
    only counts; no stored value leaves the warehouse. Columns marked PII high are skipped.
    On Spider test with every declared join hidden it recovered 123 of 145 and proposed 13 more.
    """

    def name(n: str) -> exp.Identifier:
        return exp.to_identifier(n, quoted=True)

    usable = {
        t.name: [c for c in t.columns if c.pii_risk != "high" and c.dtype != "float"] for t in catalog.tables
    }
    stats: dict[tuple[str, str], tuple[int, int, int]] = {}
    for table, cols in usable.items():
        if not cols:
            continue
        parts: list[exp.Expression] = [exp.Count(this=exp.Star())]
        for c in cols:
            parts += [exp.Count(this=exp.column(name(c.name))),
                      exp.Count(this=exp.Distinct(expressions=[exp.column(name(c.name))]))]
        row = run(exp.select(*parts).from_(exp.Table(this=name(table))).sql(dialect=catalog.sql_dialect))[0]
        for i, c in enumerate(cols):
            stats[(table, c.name)] = (int(row[0]), int(row[1 + 2 * i]), int(row[2 + 2 * i]))
    keys = [(t, c) for t, cols in usable.items() for c in cols if 0 < stats[(t, c.name)][0] == stats[(t, c.name)][2]]
    declared = {
        frozenset({(j.left_table.lower(), j.left_column.lower()), (j.right_table.lower(), j.right_column.lower())})
        for j in catalog.join_keys
    }
    found = list(catalog.join_keys)
    for table, cols in usable.items():
        for col in cols:
            if stats[(table, col.name)][2] < 2:
                continue
            for key_table, key in keys:
                if (
                    key_table == table
                    or (col.dtype == "str") != (key.dtype == "str")
                    or frozenset({(table.lower(), col.name.lower()), (key_table.lower(), key.name.lower())}) in declared
                    or not _names_link(col.name, key_table, key.name)
                ):
                    continue
                a, b = exp.column(name(col.name), table="a"), exp.column(name(key.name), table="b")
                missing = (
                    exp.select(exp.Count(this=exp.Star()))
                    .from_(exp.Table(this=name(table), alias=exp.TableAlias(this=exp.to_identifier("a"))))
                    .where(a.is_(exp.null()).not_())
                    .where(exp.Not(this=exp.Exists(this=exp.select("1").from_(
                        exp.Table(this=name(key_table), alias=exp.TableAlias(this=exp.to_identifier("b")))
                    ).where(exp.EQ(this=b, expression=a.copy())))))
                )
                if run(missing.sql(dialect=catalog.sql_dialect))[0][0] == 0:
                    found.append(JoinKey(left_table=table, left_column=col.name, right_table=key_table, right_column=key.name))
    return catalog.model_copy(update={"join_keys": found})


def catalog_drift(approved: Catalog, actual: Catalog) -> tuple[list[str], list[str]]:
    """Compare the approved catalog with what the warehouse holds now.

    Returns (breaking, new). Breaking: an approved table or column is gone or changed type,
    so approved queries will fail or mean something else. New: the warehouse has tables or
    columns the catalog does not list; they stay unqueryable until someone approves them.
    Names are compared without case.
    """
    actual_tables = {t.name.lower(): t for t in actual.tables}
    breaking: list[str] = []
    new: list[str] = []
    for table in approved.tables:
        found = actual_tables.get(table.name.lower())
        if found is None:
            breaking.append(f"table {table.name} is in the catalog but not in the warehouse")
            continue
        columns = {c.name.lower(): c for c in found.columns}
        for column in table.columns:
            match = columns.pop(column.name.lower(), None)
            if match is None:
                breaking.append(f"column {table.name}.{column.name} is in the catalog but not in the warehouse")
            elif match.dtype != column.dtype:
                breaking.append(
                    f"column {table.name}.{column.name} is {match.dtype} in the warehouse, "
                    f"{column.dtype} in the catalog"
                )
        new.extend(f"column {table.name}.{c.name} is not in the catalog" for c in columns.values())
    listed = {t.name.lower() for t in approved.tables}
    new.extend(f"table {t.name} is not in the catalog" for t in actual.tables if t.name.lower() not in listed)
    return breaking, new


def _enrich(catalog: Catalog, run: Callable[[str], Sequence[Sequence[Any]]], args: argparse.Namespace) -> Catalog:
    if args.joins and not args.check:
        before = len(catalog.join_keys)
        catalog = with_discovered_joins(catalog, run)
        for j in catalog.join_keys[before:]:
            print(f"proposed join (review): {j.left_table}.{j.left_column} -> {j.right_table}.{j.right_column}",
                  file=sys.stderr)
    return with_value_lists(catalog, run) if args.values else catalog


def _warehouse_catalog(args: argparse.Namespace) -> Catalog:
    if args.duckdb:
        import duckdb

        catalog = Catalog.model_validate(draft_from_duckdb(args.duckdb, tenant_id=args.tenant_id))
        con = duckdb.connect(str(args.duckdb), read_only=True)
        try:
            return _enrich(catalog, lambda sql: con.execute(sql).fetchall(), args)
        finally:
            con.close()
    from secure_query.engine.databricks import _connect, _safe_close, databricks_settings_from_env

    settings = databricks_settings_from_env()
    connection = _connect(**settings)
    try:
        catalog = fetch_unity_catalog_draft(
            connection, tenant_id=args.tenant_id, catalog_name=settings["catalog"], schema_name=settings["schema"]
        )

        def run(sql: str) -> Sequence[Sequence[Any]]:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                return cursor.fetchall()

        return _enrich(catalog, run, args)
    finally:
        _safe_close(connection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draft an unapproved catalog JSON, or check an approved one against the warehouse"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--duckdb", help="Path to DuckDB file")
    source.add_argument(
        "--databricks", action="store_true", help="Use DATABRICKS_* and SECURE_QUERY_DATABRICKS_SCHEMA"
    )
    parser.add_argument("--tenant-id", default="draft")
    parser.add_argument(
        "--values", action="store_true",
        help="Draft `values` for category columns (non-PII, few distinct, repeated). They are sent to the model.",
    )
    parser.add_argument(
        "--no-joins", dest="joins", action="store_false",
        help="Do not propose joins the data shows but the schema does not declare",
    )
    parser.add_argument("-o", "--output", default="-", help="Write path or - for stdout")
    parser.add_argument(
        "--check", metavar="CATALOG.json", help="Report drift from this approved catalog; exit 1 if it breaks"
    )
    args = parser.parse_args(argv)
    if args.check:
        breaking, new = catalog_drift(Catalog.from_json_file(args.check), _warehouse_catalog(args))
        for line in breaking:
            print(f"BREAKING  {line}")
        for line in new:
            print(f"new       {line}")
        print(f"{len(breaking)} breaking, {len(new)} not in the catalog")
        return 1 if breaking else 0
    payload = json.loads(_warehouse_catalog(args).model_dump_json())
    text = json.dumps(payload, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"wrote unapproved draft to {args.output}", file=sys.stderr)
        print(
            "Review PII flags, join keys and any `values` lists, then add an `approval` record "
            "(version, approved_by, approved_at) before using it as SECURE_QUERY_CATALOG_FILE.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
