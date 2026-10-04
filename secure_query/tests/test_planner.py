"""Phase 2 planner tests — mock LLM only (no network)."""

from __future__ import annotations

import json

import pytest

from secure_query.examples.sample_catalog import sample_catalog
from secure_query.planner import (
    SYSTEM_PROMPT,
    MockLLMClient,
    build_repair_prompt,
    build_user_prompt,
    parse_plan_json,
    plan_question,
    resolve_llm_settings,
)


def test_system_prompt_forbids_sql() -> None:
    assert "Never output SQL" in SYSTEM_PROMPT
    assert "LogicalPlan" in SYSTEM_PROMPT


def test_user_prompt_includes_catalog_not_rows() -> None:
    catalog = sample_catalog()
    prompt = build_user_prompt("revenue by country", catalog)
    assert "Invoice" in prompt
    assert "approved_joins" in prompt or "CustomerId" in prompt
    assert "no row data" in prompt.lower() or "Approved catalog" in prompt


def test_repair_prompt_forbids_sql() -> None:
    text = build_repair_prompt(["catalog.unknown_table: Unknown source table: 'foo'"])
    assert "Do not output SQL" in text
    assert "catalog.unknown_table" in text


def test_parse_plan_assigns_fresh_plan_id() -> None:
    raw = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "limit": 5,
        }
    )
    plan = parse_plan_json(raw)
    assert str(plan.plan_id) != "00000000-0000-0000-0000-000000000001"
    assert plan.source == "Invoice"


def test_parse_plan_rejects_sql_field() -> None:
    raw = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "sql": "SELECT * FROM Invoice",
            "limit": 5,
        }
    )
    with pytest.raises(ValueError, match="SQL"):
        parse_plan_json(raw)


def test_plan_question_ok_with_mock() -> None:
    catalog = sample_catalog()
    result = plan_question(
        "revenue by country",
        catalog,
        MockLLMClient(),
        max_repairs=1,
    )
    assert result.status == "ok"
    assert result.compiled is not None
    assert "SUM" in result.compiled.sql.upper()
    assert "Customer" in result.compiled.sql
    assert result.attempts == 1


def test_plan_question_one_repair_then_ok() -> None:
    bad = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "not_a_table",
            "limit": 10,
        }
    )
    good = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "Invoice",
            "joins": [
                {
                    "right_table": "Customer",
                    "kind": "inner",
                    "conditions": [
                        {
                            "left": {"table_id": "Invoice", "column_id": "CustomerId"},
                            "right": {"table_id": "Customer", "column_id": "CustomerId"},
                        }
                    ],
                }
            ],
            "filters": [],
            "group_by": {
                "columns": [{"table_id": "Customer", "column_id": "Country"}],
                "time_buckets": [],
            },
            "aggregations": [
                {
                    "fn": "sum",
                    "column": {"table_id": "Invoice", "column_id": "Total"},
                    "alias": "revenue",
                }
            ],
            "having": [],
            "order_by": [{"alias": "revenue", "direction": "desc"}],
            "limit": 10,
        }
    )
    client = MockLLMClient([bad, good])
    result = plan_question("revenue by country", sample_catalog(), client, max_repairs=1)
    assert result.status == "ok"
    assert result.attempts == 2
    assert len(client.calls) == 2
    assert "Do not output SQL" in client.calls[1][-1]["content"]


def test_plan_question_clarify_after_failed_repair() -> None:
    bad = json.dumps(
        {
            "plan_id": "00000000-0000-0000-0000-000000000001",
            "schema_version": "lqp/1",
            "source": "not_a_table",
            "limit": 10,
        }
    )
    client = MockLLMClient([bad, bad])
    result = plan_question("anything", sample_catalog(), client, max_repairs=1)
    assert result.status == "clarify"
    assert result.compiled is None
    assert result.clarify_message
    assert result.attempts == 2
    assert any("unknown_table" in e for e in result.errors)


def test_resolve_llm_settings_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"
    assert "11434" in settings["base_url"]


def test_resolve_llm_settings_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECURE_QUERY_PROVIDER", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "groq"
    assert "groq.com" in settings["base_url"]


def test_resolve_llm_settings_groq_ignores_ollama_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --provider groq must not send Ollama tag from .env to Groq API."""
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "qwen2.5:7b")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "groq"
    assert settings["model"] == "llama-3.3-70b-versatile"


def test_resolve_llm_settings_groq_keeps_valid_cloud_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "llama-3.3-70b-versatile")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["model"] == "llama-3.3-70b-versatile"


def test_resolve_llm_settings_ollama_ignores_groq_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "llama-3.3-70b-versatile")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"
    assert settings["model"] == "qwen2.5:7b"


def test_resolve_llm_settings_ollama_keeps_ollama_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_QUERY_PROVIDER", "ollama")
    monkeypatch.setenv("SECURE_QUERY_MODEL", "qwen2.5:7b")
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["model"] == "qwen2.5:7b"


def test_resolve_llm_settings_none_is_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "SECURE_QUERY_PROVIDER",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "SECURE_QUERY_API_KEY",
        "SECURE_QUERY_USE_OLLAMA",
        "SECURE_QUERY_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "secure_query.planner.plan._ollama_reachable", lambda host=None: False
    )
    assert resolve_llm_settings() is None


def test_resolve_auto_detects_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "SECURE_QUERY_PROVIDER",
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "SECURE_QUERY_API_KEY",
        "SECURE_QUERY_USE_OLLAMA",
        "SECURE_QUERY_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "secure_query.planner.plan._ollama_reachable", lambda host=None: True
    )
    settings = resolve_llm_settings()
    assert settings is not None
    assert settings["provider"] == "ollama"


class TestRefusal:
    """The model must have a legal way to decline, or it will substitute an answer."""

    def test_refusal_is_reported_as_a_deliberate_decline(self) -> None:
        client = MockLLMClient(
            [json.dumps({"cannot_answer": True, "reason": "No Employee table in the catalog"})]
        )
        result = plan_question("How many employees?", sample_catalog(), client)
        assert result.status == "clarify"
        assert result.refused is True
        assert result.clarify_message == "No Employee table in the catalog"

    def test_refusal_does_not_consume_a_repair_attempt(self) -> None:
        """A refusal is final; retrying only pressures the model into guessing."""
        client = MockLLMClient(
            [json.dumps({"cannot_answer": True, "reason": "no such table"})]
        )
        result = plan_question("how many employees", sample_catalog(), client, max_repairs=3)
        assert result.attempts == 1
        assert len(client.calls) == 1

    def test_employee_headcount_via_metric(self) -> None:
        result = plan_question(
            "How many employees work for us?",
            sample_catalog(),
            MockLLMClient(),
            max_repairs=1,
        )
        assert result.status == "ok"
        assert result.compiled is not None
        assert "Employee" in result.compiled.sql
        assert "COUNT" in result.compiled.sql.upper()

    def test_restricted_questions_never_reach_the_model(self) -> None:
        """PII refusal is deterministic, so the question is not sent anywhere."""
        client = MockLLMClient()
        result = plan_question("list all customer emails", sample_catalog(), client)
        assert result.refused is True
        assert result.attempts == 0
        assert client.calls == []
        assert "Customer.Email" in (result.clarify_message or "")

    def test_guard_can_be_disabled_for_ablation(self) -> None:
        client = MockLLMClient()
        result = plan_question("list all customer emails", sample_catalog(), client, guard=False)
        assert client.calls, "guard=False should let the question through to the model"

    def test_invalid_json_is_not_treated_as_a_refusal(self) -> None:
        client = MockLLMClient(["not json at all", "still not json"])
        result = plan_question("anything", sample_catalog(), client, max_repairs=1)
        assert result.status == "clarify"
        assert result.refused is False


def test_openai_client_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from openai import RateLimitError

    from secure_query.planner import OpenAIClient

    sleeps: list[float] = []
    monkeypatch.setattr("secure_query.planner.plan.time.sleep", lambda s: sleeps.append(s))

    class FakeMessage:
        content = '{"cannot_answer": true, "reason": "test"}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    calls = {"n": 0}

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            calls["n"] += 1
            if calls["n"] == 1:
                response = MagicMock()
                response.request = MagicMock()
                raise RateLimitError(
                    "Rate limit reached. Please try again in 2.5s",
                    response=response,
                    body=None,
                )
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    client = OpenAIClient(api_key="test", provider="groq")
    client._client = FakeClient()

    text = client.complete([{"role": "user", "content": "hi"}])
    assert "cannot_answer" in text
    assert calls["n"] == 2
    assert sleeps == [2.5]
