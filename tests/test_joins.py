"""Joins are resolved from catalog.join_keys; the model's joins are not trusted."""

import duckdb
import pytest

from secure_query.demo.chinook import sample_catalog
from secure_query.demo.load_chinook import DUCKDB_PATH
from secure_query.kernel.catalog import Catalog, JoinKey
from secure_query.kernel.logical_plan import (
    Aggregation,
    ColumnRef,
    Eq,
    GroupBy,
    Join,
    JoinCondition,
    LiteralValue,
    LogicalPlan,
)
from secure_query.kernel.validate import PlanValidationFailed, normalize_plan, validate_and_compile

_DB = "data/chinook.duckdb"
CATALOG = sample_catalog()


def col(t: str, c: str) -> ColumnRef:
    return ColumnRef(table_id=t, column_id=c)


def _plan(**kw) -> LogicalPlan:
    return LogicalPlan.model_validate(
        {"plan_id": "00000000-0000-0000-0000-000000000001", "limit": 10, **kw}
    )


def test_multi_hop_path_is_added_when_model_omits_joins() -> None:
    plan = _plan(
        source="InvoiceLine",
        group_by=GroupBy(columns=[col("Genre", "Name")]),
        aggregations=[Aggregation(fn="count", column=None, alias="n")],
        filters=[Eq(column=col("Artist", "Name"), value=LiteralValue(type="string", value="AC/DC"))],
    )
    resolved = normalize_plan(plan, sample_catalog())
    assert [j.right_table for j in resolved.joins] == ["Track", "Genre", "Album", "Artist"]
    compiled = validate_and_compile(plan, sample_catalog())
    duckdb.connect(_DB, read_only=True).execute(compiled.sql).fetchall()


def test_wrong_model_join_condition_is_replaced() -> None:
    bad = Join(
        right_table="Customer",
        conditions=[JoinCondition(left=col("Invoice", "InvoiceId"), right=col("Customer", "CustomerId"))],
    )
    plan = _plan(
        source="Invoice",
        joins=[bad],
        group_by=GroupBy(columns=[col("Customer", "Country")]),
        aggregations=[Aggregation(fn="sum", column=col("Invoice", "Total"), alias="revenue")],
    )
    cond = normalize_plan(plan, sample_catalog()).joins[0].conditions[0]
    assert (cond.left, cond.right) == (col("Invoice", "CustomerId"), col("Customer", "CustomerId"))


def test_idempotent() -> None:
    plan = _plan(
        source="InvoiceLine",
        group_by=GroupBy(columns=[col("Artist", "Name")]),
        aggregations=[Aggregation(fn="count", column=None, alias="n")],
    )
    once = normalize_plan(plan, sample_catalog())
    assert normalize_plan(once, sample_catalog()) == once


def _diamond(**extra) -> Catalog:
    tables = [
        {"name": n, "columns": [{"name": "id", "dtype": "int"}, {"name": "a_id", "dtype": "int"},
                                {"name": "b_id", "dtype": "int"}, {"name": "v", "dtype": "float"}]}
        for n in ("Fact", "A", "B", "Dim")
    ]
    keys = [
        JoinKey(left_table="Fact", left_column="a_id", right_table="A", right_column="id"),
        JoinKey(left_table="Fact", left_column="b_id", right_table="B", right_column="id"),
        JoinKey(left_table="A", left_column="b_id", right_table="Dim", right_column="id"),
        JoinKey(left_table="B", left_column="b_id", right_table="Dim", right_column="id"),
    ]
    return Catalog(tenant_id="t", tables=tables, join_keys=keys, **extra)


def test_two_equal_paths_are_ambiguous() -> None:
    plan = _plan(
        source="Fact",
        group_by=GroupBy(columns=[col("Dim", "v")]),
        aggregations=[Aggregation(fn="sum", column=col("Fact", "v"), alias="s")],
    )
    with pytest.raises(PlanValidationFailed, match="ambiguous_join_path"):
        normalize_plan(plan, _diamond())


def test_naming_the_bridge_table_disambiguates() -> None:
    bridge = Join(right_table="A", conditions=[JoinCondition(left=col("Fact", "a_id"), right=col("A", "id"))])
    plan = _plan(
        source="Fact",
        joins=[bridge],
        group_by=GroupBy(columns=[col("Dim", "v")]),
        aggregations=[Aggregation(fn="sum", column=col("Fact", "v"), alias="s")],
    )
    assert [j.right_table for j in normalize_plan(plan, _diamond()).joins] == ["A", "Dim"]


def test_unreachable_table_is_rejected() -> None:
    catalog = _diamond()
    catalog = catalog.model_copy(update={"join_keys": catalog.join_keys[:1]})
    plan = _plan(
        source="Fact",
        group_by=GroupBy(columns=[col("B", "v")]),
        aggregations=[Aggregation(fn="count", column=None, alias="n")],
    )
    with pytest.raises(PlanValidationFailed, match="no_join_path"):
        normalize_plan(plan, catalog)


def test_sum_over_one_side_of_fan_out_is_rejected() -> None:
    # SUM(Invoice.Total) grouped by Genre repeats each invoice once per line.
    plan = _plan(
        source="Invoice",
        group_by=GroupBy(columns=[col("Genre", "Name")]),
        aggregations=[Aggregation(fn="sum", column=col("Invoice", "Total"), alias="revenue")],
    )
    with pytest.raises(PlanValidationFailed, match="plan.fan_out"):
        validate_and_compile(plan, sample_catalog())


def test_count_distinct_across_fan_out_is_allowed() -> None:
    plan = _plan(
        source="InvoiceLine",
        filters=[Eq(column=col("Genre", "Name"), value=LiteralValue(type="string", value="Jazz"))],
        aggregations=[Aggregation(fn="count_distinct", column=col("Invoice", "CustomerId"), alias="n")],
    )
    compiled = validate_and_compile(plan, sample_catalog())
    got = duckdb.connect(_DB, read_only=True).execute(compiled.sql).fetchone()[0]
    assert got > 0



def _list_plan(source: str, filters: list[dict], order_by: list[dict] | None = None) -> LogicalPlan:
    return LogicalPlan.model_validate(
        {
            "plan_id": "00000000-0000-0000-0000-0000000000a1",
            "source": source,
            "filters": filters,
            "order_by": order_by or [],
            "limit": 500,
        }
    )


USA_BILLING = {
    "op": "eq",
    "column": {"table_id": "Invoice", "column_id": "BillingCountry"},
    "value": {"type": "string", "value": "USA"},
}


def test_list_filtered_by_child_table_keeps_parent_grain() -> None:
    """Customers with a USA invoice: one row per customer, Customer columns only."""
    sql = validate_and_compile(_list_plan("Customer", [USA_BILLING]), CATALOG).sql
    assert "EXISTS" in sql and "JOIN" not in sql.split("EXISTS")[0]
    assert '"Invoice"."Total"' not in sql
    if DUCKDB_PATH.exists():
        con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
        try:
            rows = con.execute(sql).fetchall()
            expected = con.execute(
                "SELECT COUNT(DISTINCT CustomerId) FROM Invoice WHERE BillingCountry = 'USA'"
            ).fetchone()[0]
        finally:
            con.close()
        assert len(rows) == len({r[0] for r in rows}) == expected == 13


def test_list_filter_through_several_child_hops_is_one_exists() -> None:
    rock = {
        "op": "eq",
        "column": {"table_id": "Genre", "column_id": "Name"},
        "value": {"type": "string", "value": "Rock"},
    }
    sql = validate_and_compile(_list_plan("Customer", [rock, USA_BILLING]), CATALOG).sql
    assert sql.count("EXISTS") == 1  # same invoice line, same invoice
    assert '"Genre"."Name" = \'Rock\'' in sql and "'USA'" in sql


def test_list_filtered_by_one_side_table_stays_a_join() -> None:
    usa_customer = {
        "op": "eq",
        "column": {"table_id": "Customer", "column_id": "Country"},
        "value": {"type": "string", "value": "USA"},
    }
    sql = validate_and_compile(_list_plan("Invoice", [usa_customer]), CATALOG).sql
    assert "EXISTS" not in sql and "JOIN" in sql


def test_list_sorted_by_child_column_is_rejected() -> None:
    plan = _list_plan(
        "Customer",
        [USA_BILLING],
        [{"column": {"table_id": "Invoice", "column_id": "Total"}, "direction": "desc"}],
    )
    with pytest.raises(PlanValidationFailed, match="plan.list_order_many_side"):
        validate_and_compile(plan, CATALOG)
