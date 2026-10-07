"""Authorization and principal-scoped catalog tests."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from secure_query.auth import (
    AuthError,
    Principal,
    assert_builtin_metric_allowed,
    catalog_for_principal,
    inject_row_filters,
    load_principal_registry,
    resolve_principal,
)
from secure_query.demo.chinook import sample_catalog
from secure_query.kernel.logical_plan import ColumnRef, Eq, Join, LiteralValue, LogicalPlan
from secure_query.kernel.metrics import metric_tables, metrics_for_catalog
from secure_query.kernel.validate import validate, validate_and_compile
from secure_query.planner import try_compile_metric


def test_catalog_for_principal_filters_tables() -> None:
    base = sample_catalog()
    p = Principal(
        principal_id="u1",
        tenant_id="chinook",
        allowed_tables=frozenset({"Invoice", "Customer"}),
    )
    sliced = catalog_for_principal(base, p)
    assert {t.name for t in sliced.tables} == {"Invoice", "Customer"}


def test_two_principals_cannot_cross_read() -> None:
    base = sample_catalog()
    finance = catalog_for_principal(
        base,
        Principal(
            principal_id="finance",
            tenant_id="chinook",
            allowed_tables=frozenset({"Invoice"}),
        ),
    )
    hr = catalog_for_principal(
        base,
        Principal(
            principal_id="hr",
            tenant_id="chinook",
            allowed_tables=frozenset({"Employee"}),
        ),
    )
    finance_plan = LogicalPlan(plan_id=uuid4(), source="Employee", limit=10)
    hr_plan = LogicalPlan(plan_id=uuid4(), source="Invoice", limit=10)
    assert any(e.code == "catalog.unknown_table" for e in validate(finance_plan, finance))
    assert any(e.code == "catalog.unknown_table" for e in validate(hr_plan, hr))



def test_injected_row_filter_in_compiled_sql() -> None:
    base = sample_catalog()
    catalog = catalog_for_principal(
        base,
        Principal(principal_id="u1", tenant_id="chinook", allowed_tables=frozenset({"Customer"})),
    )
    tenant_filter = Eq(
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    p = Principal(principal_id="u1", tenant_id="chinook", row_filters=(tenant_filter,))
    plan = LogicalPlan(plan_id=uuid4(), source="Customer", limit=10)
    bound = inject_row_filters(plan, p)
    compiled = validate_and_compile(bound, catalog)
    assert "USA" in compiled.sql
    assert "Country" in compiled.sql


def test_restricted_principal_does_not_see_invoice_metrics() -> None:
    base = sample_catalog()
    p = Principal(
        principal_id="restricted",
        tenant_id="chinook",
        allowed_tables=frozenset({"Genre"}),
    )
    sliced = catalog_for_principal(base, p)
    assert sliced.metric_ids == []
    plan, compiled, metric = try_compile_metric(
        json.dumps({"metric_id": "avg_revenue_per_customer"}),
        sliced,
    )
    assert plan is None
    assert compiled is None
    assert metric is None


def test_invoice_principal_keeps_invoice_metrics_only() -> None:
    base = sample_catalog()
    p = Principal(
        principal_id="finance",
        tenant_id="chinook",
        allowed_tables=frozenset({"Invoice", "Customer"}),
    )
    sliced = catalog_for_principal(base, p)
    ids = set(sliced.metric_ids)
    assert "total_revenue" in ids
    assert "avg_revenue_per_customer" in ids
    assert "employee_count" not in ids
    assert "revenue_by_genre" not in ids
    for metric in metrics_for_catalog(sliced):
        assert metric_tables(metric) <= {"Invoice", "Customer"}


def test_builtin_metric_blocked_when_row_filters_present() -> None:
    tenant_filter = Eq(
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    p = Principal(principal_id="u1", tenant_id="chinook", row_filters=(tenant_filter,))
    with pytest.raises(PermissionError, match="row filters"):
        assert_builtin_metric_allowed(p)


def test_cross_join_rejected_even_when_tables_are_allowed() -> None:
    catalog = sample_catalog()
    plan = LogicalPlan(
        plan_id=uuid4(),
        source="Customer",
        joins=[Join(right_table="Employee", kind="cross", conditions=[])],
        limit=100,
    )
    errors = validate(plan, catalog)
    assert any(e.code == "policy.cross_join_not_allowed" for e in errors)


def test_resolve_principal_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPAL_ID", "alice")
    monkeypatch.setenv("SECURE_QUERY_TENANT_ID", "chinook")
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    principal = resolve_principal()
    assert principal.principal_id == "alice"
    assert principal.tenant_id == "chinook"


def test_resolve_principal_ignores_forged_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity comes from the server, never from request JSON."""
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPAL_ID", "real-user")
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    principal = resolve_principal(authorization="Bearer forged")
    assert principal.principal_id == "real-user"


def test_token_mode_requires_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv(
        "SECURE_QUERY_API_TOKENS",
        json.dumps({"secret-alice": {"principal_id": "alice", "allowed_tables": ["Invoice"]}}),
    )
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    with pytest.raises(AuthError) as missing:
        resolve_principal()
    assert missing.value.status_code == 401
    with pytest.raises(AuthError) as bad:
        resolve_principal(authorization="Bearer nope")
    assert bad.value.status_code == 401
    principal = resolve_principal(authorization="Bearer secret-alice")
    assert principal.principal_id == "alice"
    assert principal.allowed_tables == frozenset({"Invoice"})


def test_header_mode_unknown_user_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "header")
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    with pytest.raises(AuthError) as exc:
        resolve_principal(forwarded_user="nobody")
    assert exc.value.status_code == 403


def test_principals_file_row_filters(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "tenant_id": "chinook",
                "principals": {
                    "usa-manager": {
                        "allowed_tables": ["Customer"],
                        "row_filters": [
                            {
                                "op": "eq",
                                "column": {"table_id": "Customer", "column_id": "Country"},
                                "value": {"type": "string", "value": "USA"},
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "header")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPALS_FILE", str(path))
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    principal = resolve_principal(forwarded_user="usa-manager")
    assert principal.allowed_tables == frozenset({"Customer"})
    assert len(principal.row_filters) == 1
    bound = inject_row_filters(
        LogicalPlan(plan_id=uuid4(), source="Customer", limit=10),
        principal,
    )
    assert bound.filters[0].op == "eq"  # type: ignore[union-attr]


def test_load_principal_registry_tokens_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SECURE_QUERY_API_TOKENS",
        json.dumps({"t1": "bob"}),
    )
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    registry = load_principal_registry()
    assert registry["tokens"]["t1"] == "bob"


def test_restricted_principal_cannot_make_unapproved_joins() -> None:
    """Slicing away every join key must deny joins, not fall back to allow-any."""
    from secure_query.kernel.builder import LQP

    base = sample_catalog()
    p = Principal(
        principal_id="restricted",
        tenant_id="chinook",
        allowed_tables=frozenset({"Invoice", "Employee"}),
    )
    sliced = catalog_for_principal(base, p)
    assert sliced.join_keys == []
    plan = (
        LQP.aggregate(table="Invoice")
        .join("Employee", on=[("Invoice.InvoiceId", "Employee.EmployeeId")])
        .group_by_columns(["Employee.Title"])
        .agg("sum", "Invoice.Total", alias="revenue")
        .limit(10)
        .build()
    )
    codes = {e.code for e in validate(plan, sliced)}
    assert "policy.join_not_allowed" in codes
