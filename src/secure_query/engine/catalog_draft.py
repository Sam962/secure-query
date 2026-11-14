"""Draft an unapproved Catalog from DuckDB information_schema.

A data owner must review PII flags and join keys before the draft is the
planner allowlist. This is a toil reducer, not an auto-approval pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
        return Catalog.model_validate(draft_from_duckdb(args.duckdb, tenant_id=args.tenant_id))
    from secure_query.engine.databricks import _connect, _safe_close, databricks_settings_from_env

    settings = databricks_settings_from_env()
    connection = _connect(**settings)
    try:
        return fetch_unity_catalog_draft(
            connection, tenant_id=args.tenant_id, catalog_name=settings["catalog"], schema_name=settings["schema"]
        )
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
            "Review PII flags and join keys, then add an `approval` record "
            "(version, approved_by, approved_at) before using it as SECURE_QUERY_CATALOG_FILE.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
