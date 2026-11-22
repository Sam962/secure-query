"""Adversarial planner / API inputs must never execute unvalidated SQL."""

from __future__ import annotations

import json

import pytest

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.planner import MockLLMClient, plan_sql_question

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from secure_query.api import app


def test_injected_sql_in_question_is_refused_before_llm() -> None:
    outcome = ask(
        "SELECT email FROM Customer",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        review_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "sql_in_question"
    assert outcome.suggestions == []


def test_model_sql_reading_pii_does_not_execute() -> None:
    client = MockLLMClient(responses=['{"sql": "SELECT * FROM Customer"}'])
    result = plan_sql_question("list customers", sample_catalog(), client, max_repairs=0)
    assert result.status == "clarify"
    assert result.compiled is None


def test_api_rejects_unknown_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPAL_ID", "demo-user")
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    response = TestClient(app).post(
        "/ask",
        json={"question": "hi", "sql": "SELECT 1", "review_only": True},
    )
    assert response.status_code == 422


def test_sql_planner_refuses_columns_pii_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: model SQL that uses COLUMNS('Email') must not execute."""
    from secure_query.planner.sql_plan import plan_sql_question

    client = MockLLMClient(responses=['{"sql": "SELECT COLUMNS(\'Email\') FROM Customer LIMIT 3"}'])
    result = plan_sql_question(
        "customer emails",
        sample_catalog(),
        client,
        max_repairs=0,
    )
    assert result.status == "clarify"
    assert result.compiled is None


def test_sql_planner_refuses_fan_out_cte_launder() -> None:
    from secure_query.planner.sql_plan import plan_sql_question

    sql = (
        "WITH j AS (SELECT i.Total AS t FROM Invoice i "
        "JOIN InvoiceLine il ON i.InvoiceId = il.InvoiceId) "
        "SELECT SUM(t) AS revenue FROM j"
    )
    result = plan_sql_question(
        "total invoice revenue",
        sample_catalog(),
        MockLLMClient(responses=[json.dumps({"sql": sql})]),
        max_repairs=0,
    )
    assert result.status == "clarify"
    assert result.compiled is None
