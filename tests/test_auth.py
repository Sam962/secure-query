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
from secure_query.kernel.metrics import metric_tables
from secure_query.kernel.validate import validate, validate_and_compile


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
    for metric in sliced.metrics:
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
    monkeypatch.setenv("SECURE_QUERY_TRUSTED_PROXIES", "10.0.0.0/8")
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    monkeypatch.delenv("SECURE_QUERY_PRINCIPALS_FILE", raising=False)
    with pytest.raises(AuthError) as exc:
        resolve_principal(forwarded_user="nobody", client_host="10.1.2.3")
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
    monkeypatch.setenv("SECURE_QUERY_TRUSTED_PROXIES", "10.0.0.5")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPALS_FILE", str(path))
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    principal = resolve_principal(forwarded_user="usa-manager", client_host="10.0.0.5")
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
    from secure_query.demo.lqp import LQP

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


def test_header_mode_without_trusted_proxies_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """L13: X-Forwarded-User is only safe behind a known proxy."""
    from fastapi.testclient import TestClient

    from secure_query.api import app
    from secure_query.auth import AuthConfigError, check_auth_config

    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "header")
    monkeypatch.delenv("SECURE_QUERY_TRUSTED_PROXIES", raising=False)
    with pytest.raises(AuthConfigError):
        check_auth_config()
    with pytest.raises(AuthConfigError):
        with TestClient(app):  # app startup runs the check
            pass
    monkeypatch.setenv("SECURE_QUERY_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.1.4")
    check_auth_config()


def test_production_without_signing_secret_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.auth import AuthConfigError, check_auth_config

    monkeypatch.setenv("SECURE_QUERY_ENV", "production")
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.delenv("SECURE_QUERY_REVIEW_SECRET", raising=False)
    with pytest.raises(AuthConfigError, match="SECURE_QUERY_REVIEW_SECRET"):
        check_auth_config()
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "x" * 31)
    with pytest.raises(AuthConfigError, match="SECURE_QUERY_REVIEW_SECRET"):
        check_auth_config()


def test_production_with_signing_secret_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.auth import check_auth_config

    monkeypatch.setenv("SECURE_QUERY_ENV", "production")
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    monkeypatch.setenv("SECURE_QUERY_REVIEW_SECRET", "x" * 32)
    check_auth_config()


def test_header_identity_is_ignored_unless_from_a_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "header")
    monkeypatch.setenv("SECURE_QUERY_TRUSTED_PROXIES", "10.0.0.0/8")
    for host in (None, "203.0.113.9", "not-an-ip"):
        with pytest.raises(AuthError) as exc:
            resolve_principal(forwarded_user="alice", client_host=host)
        assert exc.value.status_code == 403


def _rsa_token(private_key, **claims) -> str:
    import time

    import jwt

    body = {"iss": "https://idp.example", "aud": "secure-query", "exp": int(time.time()) + 300, **claims}
    return jwt.encode(body, private_key, algorithm="RS256")


REGISTRY = {
    "tenant_id": "acme",
    "principals": {"carol": {"allowed_tables": ["Track"]}},
    "groups": {
        "finance": {"allowed_tables": ["Invoice", "Customer"]},
        "usa": {
            "allowed_tables": ["Customer"],
            "row_filters": [
                {
                    "op": "eq",
                    "column": {"table_id": "Customer", "column_id": "Country"},
                    "value": {"type": "string", "value": "USA"},
                }
            ],
        },
    },
}


def test_jwt_mode_maps_groups_to_tables_and_keeps_every_row_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa

    import secure_query.auth as auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "jwt")
    monkeypatch.setenv("SECURE_QUERY_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("SECURE_QUERY_OIDC_AUDIENCE", "secure-query")
    monkeypatch.setattr(auth, "_jwt_signing_key", lambda token: key.public_key())
    auth.check_auth_config()

    def who(**claims) -> auth.Principal:
        return resolve_principal(authorization=f"Bearer {_rsa_token(key, **claims)}", settings=REGISTRY)

    both = who(sub="dana", groups=["finance", "usa", "unrelated"])
    assert both.principal_id == "dana" and both.tenant_id == "acme"
    assert both.allowed_tables == frozenset({"Invoice", "Customer"}) and len(both.row_filters) == 1
    assert who(sub="carol", groups=["finance"]).allowed_tables == frozenset({"Track"})  # named principal wins

    with pytest.raises(AuthError) as no_group:
        who(sub="erin", groups=["unrelated"])
    assert no_group.value.status_code == 403
    for bad in ({"sub": "dana", "groups": ["finance"], "aud": "other"}, {"sub": "dana", "exp": 1}, {"groups": ["finance"]}):
        with pytest.raises(AuthError) as denied:
            who(**bad)
        assert denied.value.status_code == 401
    forged = _rsa_token(rsa.generate_private_key(public_exponent=65537, key_size=2048), sub="dana", groups=["finance"])
    with pytest.raises(AuthError):
        resolve_principal(authorization=f"Bearer {forged}", settings=REGISTRY)


def test_jwt_mode_needs_issuer_and_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_query.auth import AuthConfigError, check_auth_config

    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "jwt")
    monkeypatch.delenv("SECURE_QUERY_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("SECURE_QUERY_OIDC_AUDIENCE", raising=False)
    with pytest.raises(AuthConfigError, match="OIDC_ISSUER"):
        check_auth_config()


def test_principals_file_is_reread_when_it_changes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import os

    from secure_query.auth import principal_registry

    path = tmp_path / "principals.json"
    path.write_text(json.dumps({"tokens": {"t1": "alice"}, "principals": {"alice": {"allowed_tables": ["Track"]}}}))
    monkeypatch.setenv("SECURE_QUERY_PRINCIPALS_FILE", str(path))
    monkeypatch.delenv("SECURE_QUERY_API_TOKENS", raising=False)
    assert principal_registry()["principals"]["alice"]["allowed_tables"] == ["Track"]
    path.write_text(json.dumps({"tokens": {}, "principals": {}}))
    os.utime(path, ns=(1, 1))  # a different modification time, whatever the clock resolution
    assert principal_registry()["tokens"] == {} and "alice" not in principal_registry()["principals"]
