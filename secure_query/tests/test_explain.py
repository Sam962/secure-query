"""Tests for plain-English plan rendering."""

from __future__ import annotations

from uuid import UUID

from secure_query.kernel.builder import LQP
from secure_query.kernel.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.kernel.explain import explain_plan
from secure_query.kernel.logical_plan import (
    Aggregation,
    Between,
    ColumnRef,
    In,
    IsNull,
    Join,
    JoinCondition,
    Like,
    LiteralValue,
    LogicalPlan,
)

PLAN_ID = UUID("12345678-1234-1234-1234-123456789abc")


def _catalog() -> Catalog:
    return Catalog(
        tenant_id="t1",
        max_limit=100,
        tables=[
            TableSpec(
                name="invoices",
                columns=[
                    ColumnSpec(name="invoice_id", dtype="int"),
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="total", dtype="float"),
                    ColumnSpec(name="invoiced_at", dtype="datetime"),
                ],
            ),
            TableSpec(
                name="customers",
                columns=[
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="country", dtype="str"),
                    ColumnSpec(name="email", dtype="str", pii_risk="high"),
                ],
            ),
        ],
        join_keys=[
            JoinKey(
                left_table="invoices",
                left_column="customer_id",
                right_table="customers",
                right_column="customer_id",
            )
        ],
    )


def test_explains_source_filter_and_limit() -> None:
    plan = (
        LQP.filter_and_list(table="customers")
        .filter("customers.country", "eq", "USA")
        .limit(20)
        .build()
    )
    text = explain_plan(plan, _catalog())
    assert "Reads customers." in text
    assert "customers.country is 'USA'" in text
    assert "Returns at most 20 rows." in text


def test_list_intent_names_returned_columns_and_omits_pii() -> None:
    plan = LQP.filter_and_list(table="customers").limit(5).build()
    text = explain_plan(plan, _catalog())
    assert "country" in text
    assert "email" not in text


def test_aggregate_states_the_grain_it_averages_over() -> None:
    """The wrong-grain error is only visible if we say what is averaged over."""
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="invoices",
        joins=[
            Join(
                right_table="customers",
                conditions=[
                    JoinCondition(
                        left=ColumnRef(table_id="invoices", column_id="customer_id"),
                        right=ColumnRef(table_id="customers", column_id="customer_id"),
                    )
                ],
            )
        ],
        aggregations=[
            Aggregation(
                fn="avg",
                column=ColumnRef(table_id="invoices", column_id="total"),
                alias="avg_revenue",
            )
        ],
        limit=10,
    )
    text = explain_plan(plan, _catalog())
    assert "average of invoices.total" in text
    assert "computed across invoices rows" in text
    assert "Returns a single row" in text


def test_group_by_reports_one_row_per_key() -> None:
    plan = (
        LQP.aggregate(table="invoices")
        .join("customers", on=[("invoices.customer_id", "customers.customer_id")])
        .group_by_columns(["customers.country"])
        .agg("sum", "invoices.total", alias="revenue")
        .order_by("revenue", direction="desc")
        .limit(10)
        .build()
    )
    text = explain_plan(plan, _catalog())
    assert "Returns one row per customers.country." in text
    assert "Sorted by revenue (highest first)." in text
    assert "matched to customers where" in text


def test_time_bucket_and_count_star() -> None:
    plan = (
        LQP.aggregate(table="invoices")
        .group_by_time("invoices.invoiced_at", grain="month")
        .agg("count", None, alias="n")
        .limit(12)
        .build()
    )
    text = explain_plan(plan, _catalog())
    assert "number of rows in invoices" in text
    assert "month of invoices.invoiced_at" in text


def test_explains_without_a_catalog() -> None:
    plan = LQP.filter_and_list(table="customers").limit(5).build()
    assert "Reads customers." in explain_plan(plan)


def test_between_and_in_filters_read_naturally() -> None:
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="invoices",
        filters=[
            Between(
                column=ColumnRef(table_id="invoices", column_id="total"),
                low=LiteralValue(type="integer", value=10),
                high=LiteralValue(type="integer", value=100),
            ),
            In(
                column=ColumnRef(table_id="invoices", column_id="customer_id"),
                values=[LiteralValue(type="integer", value=v) for v in (1, 2, 3)],
            ),
        ],
        limit=5,
    )
    text = explain_plan(plan, _catalog())
    assert "invoices.total is between 10 and 100" in text
    assert "invoices.customer_id is one of 1, 2 or 3" in text


def test_null_and_like_filters_read_naturally() -> None:
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="customers",
        filters=[
            IsNull(column=ColumnRef(table_id="customers", column_id="country")),
            Like(
                column=ColumnRef(table_id="customers", column_id="country"),
                pattern=LiteralValue(type="string", value="U%"),
            ),
        ],
        limit=5,
    )
    text = explain_plan(plan, _catalog())
    assert "customers.country is empty" in text
    assert "matches the pattern 'U%'" in text
