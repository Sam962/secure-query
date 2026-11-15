"""Catalog draft from DuckDB information_schema is unapproved."""

from __future__ import annotations

from pathlib import Path

import duckdb

from secure_query.demo.load_chinook import create_schema
from secure_query.engine.catalog_draft import draft_from_duckdb


def test_draft_from_tiny_duckdb(tmp_path: Path) -> None:
    path = tmp_path / "draft.duckdb"
    con = duckdb.connect(str(path))
    create_schema(con)
    con.close()
    payload = draft_from_duckdb(path, tenant_id="draft")
    names = {t["name"] for t in payload["tables"]}
    assert "Invoice" in names
    assert "Customer" in names
    assert payload["tenant_id"] == "draft"
    assert payload["join_keys"] == []


def test_value_lists_are_drafted_for_category_columns_only(tmp_path: Path) -> None:
    import pytest

    from secure_query.engine.catalog_draft import with_value_lists
    from secure_query.kernel.catalog import Catalog, ColumnSpec, TableSpec

    con = duckdb.connect(str(tmp_path / "shop.duckdb"))
    con.execute("CREATE TABLE item (kind VARCHAR, label VARCHAR, owner_email VARCHAR)")
    con.execute(
        "INSERT INTO item VALUES ('Cake','a','x@y'),('Cake','b','x@y'),('Tart','c','x@y'),('Tart','d','x@y')"
    )
    catalog = Catalog(
        tenant_id="t",
        tables=[
            TableSpec(
                name="item",
                columns=[
                    ColumnSpec(name="kind", dtype="str"),
                    ColumnSpec(name="label", dtype="str"),  # every value differs: names, not codes
                    ColumnSpec(name="owner_email", dtype="str", pii_risk="high"),  # never read
                ],
            )
        ],
    )
    seen: list[str] = []

    def run(sql: str):
        seen.append(sql)
        return con.execute(sql).fetchall()

    drafted = with_value_lists(catalog, run)
    kind, label, email = drafted.tables[0].columns
    assert kind.values == ("Cake", "Tart") and label.values == () and email.values == ()
    assert not any("owner_email" in sql for sql in seen)
    assert "kind: str values: 'Cake', 'Tart'" in drafted.planner_summary()
    with pytest.raises(ValueError, match="PII"):
        ColumnSpec(name="owner_email", dtype="str", pii_risk="high", values=("x@y",))
