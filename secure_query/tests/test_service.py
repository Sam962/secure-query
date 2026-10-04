"""Ask pipeline: confirm gate, SQL rejection, production auth."""

from __future__ import annotations

import pytest

from secure_query.auth import Principal
from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner import MockLLMClient
from secure_query.engine.runtime import runtime_config
from secure_query.api.service import ask, production_auth_blocked


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
