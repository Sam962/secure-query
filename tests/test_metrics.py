"""Tests for approved metrics as catalog data."""

import json

import duckdb
import pytest
from pydantic import ValidationError

from secure_query.demo.chinook import chinook_metrics, sample_catalog
from secure_query.kernel.catalog import Catalog
from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue
from secure_query.kernel.metrics import (
    MetricSpec,
    compile_builtin_metric,
    expand_metric_plan,
    get_metric,
    metric_tables,
)
from secure_query.kernel.validate import (
    PlanValidationFailed,
    validate_and_compile,
    validate_and_compile_metric,
)
from tests.conftest import requires_chinook

_DB = "data/chinook.duckdb"


def _metric(metric_id: str) -> MetricSpec:
    metric = get_metric(metric_id, sample_catalog())
    assert metric is not None
    return metric


def _tiny_catalog(**updates) -> dict:
    """Minimal catalog JSON with one table, to build catalogs from data."""
    data = {
        "tenant_id": "t",
        "tables": [
            {
                "name": "Orders",
                "columns": [
                    {"name": "Amount", "dtype": "float"},
                    {"name": "CustomerId", "dtype": "int"},
                ],
            }
        ],
    }
    data.update(updates)
    return data


def test_chinook_metrics_live_on_catalog() -> None:
    catalog = sample_catalog()
    assert [m.id for m in catalog.metrics] == [m.id for m in chinook_metrics()]
    assert catalog.metric_ids == [m.id for m in chinook_metrics()]


def test_metrics_load_from_catalog_json() -> None:
    catalog = Catalog.model_validate(
        _tiny_catalog(
            metrics=[
                {
                    "id": "avg_order_value",
                    "description": "Revenue per order",
                    "kind": "ratio",
                    "source": "Orders",
                    "numerator": "sum:Orders.Amount",
                    "denominator": "count:*",
                    "unit": "USD",
                }
            ]
        )
    )
    assert catalog.metric_ids == ["avg_order_value"]
    restored = Catalog.model_validate_json(catalog.model_dump_json())
    assert restored.metrics == catalog.metrics


def test_legacy_metric_ids_rejected_with_clear_message() -> None:
    with pytest.raises(ValidationError, match="metrics"):
        Catalog.model_validate(_tiny_catalog(metric_ids=["total_revenue"]))
    assert Catalog.model_validate(_tiny_catalog(metric_ids=[])).metrics == []


@pytest.mark.parametrize(
    ("metric", "match"),
    [
        ({"source": "Orders", "aggregations": ["sum:Orders.Missing:x"]}, "does not validate"),
        ({"source": "Nope", "aggregations": ["count:*:x"]}, "not in catalog"),
        ({"source": "Orders", "aggregations": ["sum:Orders.Amount"]}, "bad aggregation"),
        ({"source": "Orders", "kind": "ratio", "numerator": "sum:Orders.Amount"}, "denominator"),
        ({"source": "Orders", "kind": "builtin", "builder_id": "nope", "tables": ["Orders"]}, "builder_id"),
    ],
)
def test_bad_metric_definitions_fail_at_load(metric: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Catalog.model_validate(
            _tiny_catalog(metrics=[{"id": "m", "description": "d", **metric}])
        )


def test_duplicate_metric_ids_rejected() -> None:
    metric = {"id": "m", "description": "d", "source": "Orders", "aggregations": ["count:*:n"]}
    with pytest.raises(ValidationError, match="duplicate"):
        Catalog.model_validate(_tiny_catalog(metrics=[metric, metric]))


def test_total_revenue_metric_expands_and_compiles() -> None:
    plan = expand_metric_plan(_metric("total_revenue"))
    compiled = validate_and_compile(plan, sample_catalog())
    assert "SUM" in compiled.sql.upper()
    assert "Invoice" in compiled.sql


@requires_chinook
def test_ratio_metric_compiles_and_matches_reference() -> None:
    metric = _metric("avg_revenue_per_customer")
    compiled = validate_and_compile_metric(metric, expand_metric_plan(metric), sample_catalog())
    assert compiled.plan_hash.startswith("metric:avg_revenue_per_customer:")
    con = duckdb.connect(_DB, read_only=True)
    got = con.execute(compiled.sql).fetchone()[0]
    want = con.execute(
        'SELECT SUM("Total") / COUNT(DISTINCT "CustomerId") FROM "Invoice"'
    ).fetchone()[0]
    assert got == pytest.approx(want)


@requires_chinook
def test_ratio_metric_applies_row_filters() -> None:
    metric = _metric("avg_revenue_per_customer")
    usa_only = Eq(
        column=ColumnRef(table_id="Invoice", column_id="BillingCountry"),
        value=LiteralValue(type="string", value="USA"),
    )
    plan = expand_metric_plan(metric)
    plan = plan.model_copy(update={"filters": [usa_only]})
    compiled = validate_and_compile_metric(metric, plan, sample_catalog())
    assert "WHERE" in compiled.sql and "'USA'" in compiled.sql
    con = duckdb.connect(_DB, read_only=True)
    got = con.execute(compiled.sql).fetchone()[0]
    want = con.execute(
        'SELECT SUM("Total") / COUNT(DISTINCT "CustomerId") FROM "Invoice" '
        "WHERE \"BillingCountry\" = 'USA'"
    ).fetchone()[0]
    assert got == pytest.approx(want)


@requires_chinook
def test_ratio_metric_row_filter_on_parent_table_joins_via_catalog() -> None:
    """A row filter on a to-one parent is applied through the approved join."""
    metric = _metric("avg_revenue_per_customer")
    other_table = Eq(
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    plan = expand_metric_plan(metric).model_copy(update={"filters": [other_table]})
    compiled = validate_and_compile_metric(metric, plan, sample_catalog())
    con = duckdb.connect(_DB, read_only=True)
    got = con.execute(compiled.sql).fetchone()[0]
    want = con.execute(
        'SELECT SUM(i."Total") / COUNT(DISTINCT i."CustomerId") FROM "Invoice" i '
        'JOIN "Customer" c ON i."CustomerId" = c."CustomerId" WHERE c."Country" = \'USA\''
    ).fetchone()[0]
    assert got == pytest.approx(want)


def test_ratio_metric_row_filter_on_child_table_fails_closed() -> None:
    """A row filter on a one-to-many child would inflate SUM(Total): refuse."""
    metric = _metric("avg_revenue_per_customer")
    child = Eq(
        column=ColumnRef(table_id="InvoiceLine", column_id="Quantity"),
        value=LiteralValue(type="integer", value=1),
    )
    plan = expand_metric_plan(metric).model_copy(update={"filters": [child]})
    with pytest.raises(PlanValidationFailed, match="plan.fan_out"):
        validate_and_compile_metric(metric, plan, sample_catalog())


def test_grouped_ratio_orders_by_ratio_and_guards_zero() -> None:
    catalog = Catalog.model_validate(
        _tiny_catalog(
            metrics=[
                {
                    "id": "amount_per_order_by_customer",
                    "description": "d",
                    "kind": "ratio",
                    "source": "Orders",
                    "group_by": ["Orders.CustomerId"],
                    "numerator": "sum:Orders.Amount",
                    "denominator": "count:*",
                }
            ]
        )
    )
    metric = catalog.metrics[0]
    sql = validate_and_compile_metric(metric, expand_metric_plan(metric), catalog).sql
    assert "NULLIF" in sql
    assert 'GROUP BY "Orders"."CustomerId"' in sql
    assert 'ORDER BY "amount_per_order_by_customer" DESC' in sql


def test_metric_tables_for_ratio_and_joins() -> None:
    assert metric_tables(_metric("avg_revenue_per_customer")) == frozenset({"Invoice"})
    assert "Genre" in metric_tables(_metric("revenue_by_genre"))
    assert "Employee" in metric_tables(_metric("employee_count"))


def test_line_item_revenue_builtin_compiles_product_via_ast() -> None:
    metric = _metric("line_item_revenue")
    assert metric.builder_id is not None
    sql = compile_builtin_metric(metric.builder_id)
    assert "UnitPrice" in sql
    assert "Quantity" in sql
    assert "*" in sql


def test_try_compile_metric_returns_ratio_plan() -> None:
    from secure_query.planner import try_compile_metric

    plan, compiled, metric = try_compile_metric(
        json.dumps({"metric_id": "avg_revenue_per_customer"}), sample_catalog()
    )
    assert metric is not None and metric.kind == "ratio"
    assert plan is not None and compiled is not None


def test_employee_count_metric_expands_and_compiles() -> None:
    plan = expand_metric_plan(_metric("employee_count"))
    compiled = validate_and_compile(plan, sample_catalog())
    assert "COUNT" in compiled.sql.upper()
    assert "Employee" in compiled.sql
