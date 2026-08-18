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
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.guard import useless_joins
from secure_query.validate import PlanValidationFailed, normalize_plan, validate, validate_and_compile

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


def test_column_from_unjoined_table_rejected() -> None:
    catalog = sample_catalog()
    plan = (
        LQP.aggregate(table="Album")
        .group_by_columns(["Artist.Name"])
        .agg("count", None, alias="album_count")
        .limit(5)
        .build()
    )
    errors = validate(plan, catalog)
    assert any(e.code == "plan.table_not_in_scope" for e in errors)


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


def test_high_pii_filter_blocked_even_when_aggregating() -> None:
    """Filtering on PII is an existence oracle even if only counts are returned."""
    plan = (
        LQP.aggregate(table="customers")
        .filter("customers.email", "eq", "a@b.com")
        .agg("count", None, alias="n")
        .limit(1)
        .build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.pii_filter_blocked" for e in errors)


def test_high_pii_group_by_blocked() -> None:
    plan = (
        LQP.aggregate(table="customers")
        .group_by_columns(["customers.email"])
        .agg("count", None, alias="n")
        .limit(10)
        .build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.pii_exposed" for e in errors)


def test_high_pii_order_by_blocked() -> None:
    plan = (
        LQP.filter_and_list(table="customers")
        .order_by("customers.email", direction="asc")
        .limit(10)
        .build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.pii_exposed" for e in errors)


@pytest.mark.parametrize("fn", ["min", "max"])
def test_high_pii_value_returning_agg_blocked(fn: str) -> None:
    plan = (
        LQP.aggregate(table="customers")
        .agg(fn, "customers.email", alias="e")
        .limit(1)
        .build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.pii_exposed" for e in errors)


def test_count_distinct_on_pii_allowed() -> None:
    """Cardinality leaks no stored value, so it stays permitted."""
    plan = (
        LQP.aggregate(table="customers")
        .agg("count_distinct", "customers.email", alias="n")
        .limit(1)
        .build()
    )
    assert validate(plan, _catalog()) == []


def test_list_intent_projects_columns_excluding_pii() -> None:
    plan = LQP.filter_and_list(table="customers").limit(10).build()
    compiled = validate_and_compile(plan, _catalog())
    assert "*" not in compiled.sql
    assert '"customers"."region"' in compiled.sql
    assert "email" not in compiled.sql


def test_alias_may_not_impersonate_a_restricted_column() -> None:
    """COUNT(*) AS "email" is safe data wearing a misleading label."""
    plan = (
        LQP.aggregate(table="customers").agg("count", None, alias="email").limit(1).build()
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.misleading_alias" for e in errors)


@pytest.mark.parametrize("alias", ["email", "Email", "email_count", "customer_emails"])
def test_compound_aliases_cannot_smuggle_a_restricted_name(alias: str) -> None:
    plan = LQP.aggregate(table="customers").agg("count", None, alias=alias).limit(1).build()
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.misleading_alias" for e in errors)


def test_ordinary_alias_is_fine() -> None:
    plan = (
        LQP.aggregate(table="customers").agg("count", None, alias="customer_count").limit(1).build()
    )
    assert validate(plan, _catalog()) == []


def test_all_pii_table_has_nothing_to_return() -> None:
    catalog = Catalog(
        tenant_id="t1",
        tables=[
            TableSpec(
                name="secrets",
                columns=[ColumnSpec(name="ssn", dtype="str", pii_risk="high")],
            )
        ],
    )
    plan = LogicalPlan(plan_id=PLAN_ID, source="secrets", limit=5)
    errors = validate(plan, catalog)
    assert any(e.code == "policy.no_selectable_columns" for e in errors)


class TestUngroupedLabelAggregate:
    """MAX(label) beside a real aggregate fakes a per-group answer."""

    def test_max_on_text_without_grouping_rejected(self) -> None:
        plan = (
            LQP.aggregate(table="customers")
            .agg("max", "customers.region", alias="top_region")
            .agg("count", None, alias="n")
            .limit(1)
            .build()
        )
        errors = validate(plan, _catalog())
        assert any(e.code == "policy.ungrouped_label_aggregate" for e in errors)

    def test_allowed_when_the_plan_actually_groups(self) -> None:
        plan = (
            LQP.aggregate(table="customers")
            .group_by_columns(["customers.customer_id"])
            .agg("max", "customers.region", alias="a_region")
            .limit(10)
            .build()
        )
        assert validate(plan, _catalog()) == []

    def test_numeric_min_max_still_allowed(self) -> None:
        plan = (
            LQP.aggregate(table="orders").agg("max", "orders.amount", alias="biggest").limit(1).build()
        )
        assert validate(plan, _catalog()) == []


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


class TestNormalizePlan:
    """Catalog-driven rewrites for joined lookups and idle joins."""

    def test_infers_group_by_when_lookup_is_joined_but_unread(self) -> None:
        """Album JOIN Artist with COUNT(*) but no GROUP BY → group by Artist.Name."""
        catalog = sample_catalog()
        raw = (
            LQP.aggregate(table="Album")
            .join("Artist", on=[("Album.ArtistId", "Artist.ArtistId")])
            .agg("count", None, alias="album_count")
            .order_by("album_count", direction="desc")
            .limit(1)
            .build()
        )
        plan = normalize_plan(raw, catalog)
        assert useless_joins(plan) is None
        assert plan.group_by is not None
        assert ColumnRef(table_id="Artist", column_id="Name") in plan.group_by.columns

    def test_count_join_chain_not_flagged_when_grouping_lookup_label(self) -> None:
        """Extra hops on a COUNT plan must not trip useless_joins if they multiply rows."""
        catalog = sample_catalog()
        raw = (
            LQP.aggregate(table="Genre")
            .join("Track", on=[("Genre.GenreId", "Track.GenreId")])
            .join("InvoiceLine", on=[("Track.TrackId", "InvoiceLine.TrackId")])
            .join("Invoice", on=[("InvoiceLine.InvoiceId", "Invoice.InvoiceId")])
            .join("Customer", on=[("Invoice.CustomerId", "Customer.CustomerId")])
            .join("Employee", on=[("Customer.SupportRepId", "Employee.EmployeeId")])
            .group_by_columns(["Genre.Name"])
            .agg("count", None, alias="track_count")
            .order_by("track_count", direction="desc")
            .limit(1)
            .build()
        )
        assert useless_joins(raw) is None

    def test_rewrites_fk_group_key_to_label_when_lookup_is_joined(self) -> None:
        catalog = sample_catalog()
        raw = (
            LQP.aggregate(table="Album")
            .join("Artist", on=[("Album.ArtistId", "Artist.ArtistId")])
            .group_by_columns(["Album.ArtistId"])
            .agg("count", None, alias="album_count")
            .limit(10)
            .build()
        )
        plan = normalize_plan(raw, catalog)
        assert ColumnRef(table_id="Artist", column_id="Name") in plan.group_by.columns  # type: ignore[union-attr]
        assert ColumnRef(table_id="Album", column_id="ArtistId") not in plan.group_by.columns  # type: ignore[union-attr]


def test_cross_join_rejected_by_policy() -> None:
    from secure_query.logical_plan import Join

    plan = LogicalPlan(
        plan_id=PLAN_ID,
        source="orders",
        joins=[Join(right_table="customers", kind="cross", conditions=[])],
        limit=10,
    )
    errors = validate(plan, _catalog())
    assert any(e.code == "policy.cross_join_not_allowed" for e in errors)

