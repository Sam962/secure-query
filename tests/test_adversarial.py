"""Adversarial planner / API inputs must never execute unvalidated SQL."""

from __future__ import annotations

import json

import pytest

from secure_query.api.service import ask
from secure_query.auth import Principal
from secure_query.demo.chinook import sample_catalog
from secure_query.engine.runtime import runtime_config
from secure_query.planner import MockLLMClient, parse_plan_json, plan_question

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from secure_query.api import app


def test_parse_rejects_select_in_string_field() -> None:
    raw = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "limit": 5,
            "note": "SELECT * FROM Employee",
        }
    )
    with pytest.raises(ValueError, match="SQL"):
        parse_plan_json(raw)


def test_injected_sql_in_question_is_refused_before_llm() -> None:
    outcome = ask(
        "SELECT email FROM Customer",
        principal=Principal(principal_id="demo-user", tenant_id="chinook"),
        config=runtime_config(),
        catalog=sample_catalog(),
        client=MockLLMClient(),
        confirm_only=True,
    )
    assert outcome.status == "clarify"
    assert outcome.clarify_code == "sql_in_question"
    assert outcome.suggestions == []


def test_model_sql_payload_does_not_execute() -> None:
    client = MockLLMClient(responses=['{"sql": "SELECT * FROM Customer"}'])
    result = plan_question("list customers", sample_catalog(), client, max_repairs=0)
    assert result.status == "clarify"
    assert result.plan is None
    assert result.compiled is None


def test_api_rejects_unknown_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_AUTH_MODE", "dev")
    monkeypatch.setenv("SECURE_QUERY_PRINCIPAL_ID", "demo-user")
    monkeypatch.setattr("secure_query.api.http.get_client", lambda: MockLLMClient())
    response = TestClient(app).post(
        "/ask",
        json={"question": "hi", "sql": "SELECT 1", "confirm_only": True},
    )
    assert response.status_code == 422
