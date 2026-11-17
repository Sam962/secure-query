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


def test_joins_the_data_shows_are_proposed_and_nothing_else(tmp_path: Path) -> None:
    from secure_query.engine.catalog_draft import with_discovered_joins
    from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec

    con = duckdb.connect(str(tmp_path / "shop.duckdb"))
    con.execute("CREATE TABLE customers (id INT, email VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1,'a@x'),(2,'b@x'),(3,'c@x')")
    con.execute("CREATE TABLE orders (order_id INT, customer_id INT, store_id INT, email VARCHAR)")
    # customer_id: every value is a customer. store_id: names a table that is not there.
    con.execute("INSERT INTO orders VALUES (10,1,1,'a@x'),(11,2,2,'b@x'),(12,2,3,'b@x')")
    con.execute("CREATE TABLE refunds (refund_id INT, order_id INT)")
    con.execute("INSERT INTO refunds VALUES (1,10),(2,99)")  # 99 is no order: not a join
    catalog = Catalog(
        tenant_id="t",
        tables=[
            TableSpec(name="customers", columns=[ColumnSpec(name="id", dtype="int"),
                                                 ColumnSpec(name="email", dtype="str", pii_risk="high")]),
            TableSpec(name="orders", columns=[ColumnSpec(name="order_id", dtype="int"),
                                              ColumnSpec(name="customer_id", dtype="int"),
                                              ColumnSpec(name="store_id", dtype="int"),
                                              ColumnSpec(name="email", dtype="str", pii_risk="high")]),
            TableSpec(name="refunds", columns=[ColumnSpec(name="refund_id", dtype="int"),
                                               ColumnSpec(name="order_id", dtype="int")]),
        ],
    )
    seen: list[str] = []

    def run(sql: str):
        seen.append(sql)
        return con.execute(sql).fetchall()

    drafted = with_discovered_joins(catalog, run)
    assert drafted.join_keys == [
        JoinKey(left_table="orders", left_column="customer_id", right_table="customers", right_column="id")
    ]
    assert not any("email" in sql for sql in seen)  # PII columns are never read, not even counted
    assert with_discovered_joins(drafted, run).join_keys == drafted.join_keys  # declared joins are kept once
