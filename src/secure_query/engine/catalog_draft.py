"""Draft an unapproved Catalog from DuckDB information_schema.

A data owner must review PII flags and join keys before the draft is the
planner allowlist. This is a toil reducer, not an auto-approval pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlglot import exp

from secure_query.engine.databricks import draft_catalog, fetch_unity_catalog_draft, map_uc_dtype
from secure_query.kernel.catalog import Catalog


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


def _warehouse_catalog(args: argparse.Namespace) -> Catalog:
    if args.duckdb:
        import duckdb

        catalog = Catalog.model_validate(draft_from_duckdb(args.duckdb, tenant_id=args.tenant_id))
        if not args.values:
            return catalog
        con = duckdb.connect(str(args.duckdb), read_only=True)
        try:
            return with_value_lists(catalog, lambda sql: con.execute(sql).fetchall())
        finally:
            con.close()
    from secure_query.engine.databricks import _connect, _safe_close, databricks_settings_from_env

    settings = databricks_settings_from_env()
    connection = _connect(**settings)
    try:
        catalog = fetch_unity_catalog_draft(
            connection, tenant_id=args.tenant_id, catalog_name=settings["catalog"], schema_name=settings["schema"]
        )
        if not args.values:
            return catalog

        def run(sql: str) -> Sequence[Sequence[Any]]:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                return cursor.fetchall()

        return with_value_lists(catalog, run)
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
