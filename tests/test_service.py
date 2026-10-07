"""Ask pipeline: confirm gate, SQL rejection, production auth."""

from __future__ import annotations

import pytest

from secure_query.api.service import ask, production_auth_blocked
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.planner import MockLLMClient


def test_confirm_only_does_not_need_the_database() -> None:
    outcome = ask(
        "revenue by country",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.status == "confirm"
    assert outcome.sql
    assert "SUM" in outcome.sql.upper()
    assert outcome.rows == []


def test_empty_question_is_invalid() -> None:
    outcome = ask(
        "  ",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "invalid_request"
    assert outcome.suggestions


def test_production_blocks_dev_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_ENV", "production")
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    assert production_auth_blocked() is not None
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "token")
    assert production_auth_blocked() is None


def test_ratio_metric_runs_under_principal_row_filters() -> None:
    """Ratio metrics used to be refused for row-filtered principals; now the filter applies."""
    import json

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa_only = Eq(
        column=ColumnRef(table_id="Invoice", column_id="BillingCountry"),
        value=LiteralValue(type="string", value="USA"),
    )
    outcome = ask(
        "average revenue per customer",
        principal=Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,)),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(responses=[json.dumps({"metric_id": "avg_revenue_per_customer"})]),
        confirm_only=True,
    )
    assert outcome.status == "confirm", outcome.clarify_message
    assert outcome.sql is not None
    assert "NULLIF" in outcome.sql
    assert "'USA'" in outcome.sql
    assert outcome.explanation.startswith("Approved metric avg_revenue_per_customer")


def test_builtin_metric_refused_under_principal_row_filters() -> None:
    import json

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa_only = Eq(
        column=ColumnRef(table_id="Invoice", column_id="BillingCountry"),
        value=LiteralValue(type="string", value="USA"),
    )
    outcome = ask(
        "line item revenue",
        principal=Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,)),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(responses=[json.dumps({"metric_id": "line_item_revenue"})]),
        confirm_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "auth"


def test_sql_planner_mode_applies_row_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue
    from secure_query.planner import MockLLMClient

    monkeypatch.setenv("SECURE_QUERY_PLANNER", "sql")
    usa_only = Eq(
        op="eq",
        column=ColumnRef(table_id="Customer", column_id="Country"),
        value=LiteralValue(type="string", value="USA"),
    )
    outcome = ask(
        "How many invoices are there?",
        principal=Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,)),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient([json.dumps({"sql": "SELECT COUNT(*) FROM Invoice"})]),
        confirm_only=True,
    )
    assert outcome.status == "confirm"
    assert "EXISTS" in outcome.sql and "'USA'" in outcome.sql


def test_reviewed_plan_gets_row_filters_from_the_server() -> None:
    """The review payload carries the plan before row filters; execute re-injects them,
    so a client cannot drop the principal's filter and keep the hash."""
    import json

    from secure_query.kernel.logical_plan import ColumnRef, Eq, LiteralValue

    usa_only = Eq(
        column=ColumnRef(table_id="Invoice", column_id="BillingCountry"),
        value=LiteralValue(type="string", value="USA"),
    )
    principal = Principal(principal_id="us-team", tenant_id="chinook", row_filters=(usa_only,))
    plan = {
        "source": "Invoice",
        "filters": [],
        "group_by": None,
        "aggregations": [{"fn": "count", "column": None, "alias": "invoice_count"}],
        "having": [],
        "order_by": [],
        "limit": 1,
    }
    reviewed = ask(
        "How many invoices are there?",
        principal=principal,
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient([json.dumps(plan)]),
        confirm_only=True,
    )
    assert reviewed.status == "confirm", reviewed.clarify_message
    assert reviewed.review["plan"]["filters"] == []  # model plan, before injection
    assert "'USA'" in reviewed.sql

    from secure_query.api.service import _recompile, catalog_for_principal

    compiled, _, _, _ = _recompile(
        catalog_for_principal(sample_catalog(), principal),
        principal,
        plan=reviewed.review["plan"],
        sql=None,
        metric_id=None,
    )
    assert compiled.plan_hash == reviewed.review["plan_hash"]
    assert "'USA'" in compiled.sql
