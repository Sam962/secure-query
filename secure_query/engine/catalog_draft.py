"""Draft an unapproved Catalog from DuckDB information_schema.

A data owner must review PII flags and join keys before the draft is the
planner allowlist. This is a toil reducer, not an auto-approval pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from secure_query.engine.databricks import draft_catalog, map_uc_dtype


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft an unapproved catalog JSON")
    parser.add_argument("--duckdb", required=True, help="Path to DuckDB file")
    parser.add_argument("--tenant-id", default="draft")
    parser.add_argument("-o", "--output", default="-", help="Write path or - for stdout")
    args = parser.parse_args(argv)
    payload = draft_from_duckdb(args.duckdb, tenant_id=args.tenant_id)
    text = json.dumps(payload, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"wrote unapproved draft to {args.output}", file=sys.stderr)
        print(
            "Review PII flags and join keys before using as SECURE_QUERY_CATALOG_FILE.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
