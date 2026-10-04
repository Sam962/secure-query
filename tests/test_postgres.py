"""Postgres dialect compile parity (no live server required)."""

from __future__ import annotations

import sqlglot

from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.builder import LQP
from secure_query.kernel.compile import compile
from secure_query.kernel.metrics import expand_metric_plan, get_metric
from secure_query.kernel.validate import validate_and_compile, validate_and_compile_metric


def test_revenue_plan_compiles_as_postgres() -> None:
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
        .group_by_columns(["Customer.Country"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(10)
        .build()
    )
    compiled = compile(plan, dialect="postgres")
    parsed = sqlglot.parse(compiled.sql, dialect="postgres")
    assert parsed
    assert "SUM" in compiled.sql.upper()


def test_validate_and_compile_respects_postgres_dialect() -> None:
    catalog = sample_catalog().model_copy(update={"sql_dialect": "postgres"})
    plan = (
        LQP.aggregate(table="Invoice")
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(1)
        .build()
    )
    compiled = validate_and_compile(plan, catalog)
    sqlglot.parse(compiled.sql, dialect="postgres")


def test_ratio_metric_postgres_dialect() -> None:
    catalog = sample_catalog().model_copy(update={"sql_dialect": "postgres"})
    metric = get_metric("avg_revenue_per_customer", catalog)
    assert metric is not None
    sql = validate_and_compile_metric(metric, expand_metric_plan(metric), catalog).sql
    sqlglot.parse(sql, dialect="postgres")
    assert "CustomerId" in sql
    assert "DOUBLE PRECISION" in sql
