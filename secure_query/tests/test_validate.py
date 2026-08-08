"""Tests for catalog validation gate."""

from __future__ import annotations

from uuid import UUID

import pytest

from secure_query.builder import LQP
from secure_query.catalog import Catalog, ColumnSpec, JoinKey, TableSpec
from secure_query.logical_plan import (
    Aggregation,
    ColumnRef,
    Eq,
    LiteralValue,
    LogicalPlan,
)
from secure_query.validate import PlanValidationFailed, validate, validate_and_compile

PLAN_ID = UUID("12345678-1234-1234-1234-123456789abc")


def _catalog(*, require_limit: bool = True) -> Catalog:
    return Catalog(
        tenant_id="t1",
        require_limit=require_limit,
        max_limit=100,
        tables=[
            TableSpec(
                name="orders",
                columns=[
                    ColumnSpec(name="amount", dtype="float"),
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="status", dtype="str"),
                ],
            ),
            TableSpec(
                name="customers",
                columns=[
                    ColumnSpec(name="customer_id", dtype="int"),
                    ColumnSpec(name="region", dtype="str"),
                    ColumnSpec(name="email", dtype="str", pii_risk="high"),
                ],
            ),
        ],
        join_keys=[
            JoinKey(
                left_table="orders",
                left_column="customer_id",
                right_table="customers",
                right_column="customer_id",
            )
        ],
    )


def test_unknown_table_rejected() -> None:
    plan = LogicalPlan(plan_id=PLAN_ID, source="nope", limit=10)
    errors = validate(plan, _catalog())
    assert any(e.code == "catalog.unknown_table" for e in errors)


def test_unknown_column_rejected() -> None:
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="orders",
        filters=[
            Eq(
                column=ColumnRef(table_id="orders", column_id="not_a_col"),
                value=LiteralValue(type="string", value="x"),
            )
        ],
        limit=10,
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "catalog.unknown_column" for e in errors)


def test_literal_type_mismatch_rejected() -> None:
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="orders",
        filters=[
            Eq(
                column=ColumnRef(table_id="orders", column_id="amount"),
                value=LiteralValue(type="string", value="big"),
            )
        ],
        limit=10,
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "typecheck.literal_mismatch" for e in errors)


def test_sum_on_string_rejected() -> None:
    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="orders",
        aggregations=[
            Aggregation(
                fn="sum",
                column=ColumnRef(table_id="orders", column_id="status"),
                alias="bad",
            )
        ],
        limit=10,
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "typecheck.agg_non_numeric" for e in errors)


def test_disallowed_join_rejected() -> None:
    plan = (
        LQP.aggregate(table="orders")
        .join("customers", on=[("orders.status", "customers.region")])
        .agg("sum", "orders.amount", alias="total")
        .limit(10)
        .build()
    )
    # force stable id not required
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.join_not_allowed" for e in errors)


def test_limit_required() -> None:
    plan = LogicalPlan(plan_id=PLAN_ID, source="orders")
    errors = validate(plan, _catalog(require_limit=True))
    assert any(e.code == "policy.limit_required" for e in errors)


def test_high_pii_list_filter_blocked() -> None:
    plan = (
        LQP.filter_and_list(table="customers")
        .filter("customers.email", "eq", "a@b.com")
        .limit(10)
        .build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.pii_filter_blocked" for e in errors)


def test_valid_plan_compiles() -> None:
    plan = (
        LQP.aggregate(table="orders")
        .join("customers", on=[("orders.customer_id", "customers.customer_id")])
        .filter("customers.region", "eq", "west")
        .group_by_columns(["customers.region"])
        .agg("sum", "orders.amount", alias="total_amount")
        .limit(10)
        .build()
    )
    compiled = validate_and_compile(plan, _catalog())
    assert "SUM" in compiled.sql.upper()
    assert compiled.plan_hash
    assert compiled.sql_hash


def test_validate_and_compile_raises() -> None:
    plan = LogicalPlan(plan_id=PLAN_ID, source="missing", limit=5)
    with pytest.raises(PlanValidationFailed) as exc:
        validate_and_compile(plan, _catalog())
    assert exc.value.errors
