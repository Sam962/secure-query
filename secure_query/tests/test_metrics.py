"""Tests for approved metric registry."""

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.metrics import (
    chinook_metrics,
    compile_ratio_metric,
    expand_metric_plan,
    metric_tables,
    metrics_for_catalog,
)
from secure_query.validate import validate_and_compile


def test_chinook_metrics_registered_on_catalog() -> None:
    catalog = sample_catalog()
    assert len(metrics_for_catalog(catalog)) == len(chinook_metrics())


def test_total_revenue_metric_expands_and_compiles() -> None:
    catalog = sample_catalog()
    metric = next(m for m in chinook_metrics() if m.id == "total_revenue")
    plan = expand_metric_plan(metric)
    compiled = validate_and_compile(plan, catalog)
    assert "SUM" in compiled.sql.upper()
    assert "Invoice" in compiled.sql


def test_avg_revenue_per_customer_ratio_compiles_via_ast() -> None:
    sql = compile_ratio_metric("avg_revenue_per_customer")
    assert "SUM" in sql.upper()
    assert "DISTINCT" in sql.upper()
    assert "CustomerId" in sql


def test_metric_tables_for_ratio_and_joins() -> None:
    metrics = {m.id: m for m in chinook_metrics()}
    assert metric_tables(metrics["avg_revenue_per_customer"]) == frozenset({"Invoice"})
    assert "Genre" in metric_tables(metrics["revenue_by_genre"])
    assert "Employee" in metric_tables(metrics["employee_count"])


def test_employee_count_metric_expands_and_compiles() -> None:
    catalog = sample_catalog()
    metric = next(m for m in chinook_metrics() if m.id == "employee_count")
    plan = expand_metric_plan(metric)
    compiled = validate_and_compile(plan, catalog)
    assert "COUNT" in compiled.sql.upper()
    assert "Employee" in compiled.sql
